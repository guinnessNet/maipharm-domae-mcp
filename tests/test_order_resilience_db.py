"""격리된 로컬 PostgreSQL에서 실제 커밋/rollback과 NULL 보호를 검증한다."""
import os
import sys
import uuid
sys.path.insert(0, 'src')
import psycopg2
import pytest
from domae_mcp.cloud import scheduler as sch
from domae_mcp.cloud.fallback_db import FallbackRecorder
from domae_mcp.cloud.fallback import run_fallback
from domae_mcp.core.crawlers.base import SearchResult, OrderResult


@pytest.fixture
def database():
    dsn=os.environ.get('DOMAE_TEST_DATABASE_URL')
    if not dsn:
        pytest.skip('격리된 로컬 DB DSN을 지정해야 하는 통합 테스트')
    schema='resilience_'+uuid.uuid4().hex
    origin=psycopg2.connect(dsn)
    cur=origin.cursor()
    cur.execute(f'CREATE SCHEMA {schema}')
    cur.execute(f'SET search_path TO {schema}')
    cur.execute('''CREATE TABLE domae_cloud_orders (
      id text PRIMARY KEY, "monitorId" text, "batchId" text, supplier text,
      "productName" text, unit text, "insuranceCode" text, quantity integer, price integer,
      success boolean, "productId" text, "orderId" text, message text,
      "reasonCode" text, "orderedAt" timestamp, "adjustedQuantity" integer, "availableStock" integer)''')
    cur.execute('''CREATE TABLE domae_order_batches (
      id text PRIMARY KEY, status text, "completedAt" timestamp, "successCount" integer,
      "failCount" integer, "adjustedCount" integer, "missingQuantity" integer)''')
    cur.execute("INSERT INTO domae_order_batches (id,status) VALUES ('b','processing')")
    origin.commit()
    recorder=psycopg2.connect(dsn)
    recorder.cursor().execute(f'SET search_path TO {schema}')
    recorder.commit()
    try:
        yield origin,recorder
    finally:
        recorder.close()
        origin.rollback()
        origin.cursor().execute(f'DROP SCHEMA {schema} CASCADE')
        origin.commit()
        origin.close()


def test_original_marker_survives_sql_failure_and_prevents_retry(database):
    origin,observer=database
    cur=origin.cursor()
    cur.execute('''INSERT INTO domae_cloud_orders (id,"batchId",supplier,success)
                   VALUES ('o','b','인천',NULL),('p','b','백제',NULL)''')
    origin.commit()
    sch._mark_sending(origin,cur,'b','인천')
    with pytest.raises(psycopg2.Error):
        cur.execute('SELECT 1/0')
    origin.rollback()
    sch._fail_pending_rows(cur,'b','SQL failure')
    origin.commit()
    c=observer.cursor()
    c.execute('SELECT id,success,"reasonCode" FROM domae_cloud_orders ORDER BY id')
    assert c.fetchall()==[('o',None,'send_unknown'),('p',False,None)]
    assert sch._finalize_if_confirmed(origin,cur,'b','retry blocked')
    c.execute("SELECT success FROM domae_cloud_orders WHERE id='o'")
    assert c.fetchone()==(None,)


def test_only_unconfirmed_rows_block_rerun(database):
    origin,_=database
    cur=origin.cursor()
    cur.execute('''INSERT INTO domae_cloud_orders (id,"batchId",success,"reasonCode")
                   VALUES ('u','b',NULL,'send_unknown')''')
    origin.commit()
    assert sch._finalize_if_confirmed(origin,cur,'b','unknown only')
    cur.execute("SELECT success FROM domae_cloud_orders WHERE id='u'")
    assert cur.fetchone()==(None,)


def test_fallback_success_record_sql_failure_preserves_null_and_origin_connection(database):
    origin,secondary=database
    pick=SearchResult(maker='',product_name='베아놀',unit='12EA',insurance_code='694003321',quantity=30,supplier='복산',price=1000,product_id='p')
    # 성공 결과 기록에서만 DB 오류가 난다. pending INSERT는 실제로 커밋된다.
    def broken_record(cur,*a,**kw):
        cur.execute('SELECT 1/0')
    rec=FallbackRecorder(secondary,'m','b','인천',broken_record,id_fn=lambda:'f',now_fn=lambda:None)
    calls=[]
    class Crawler:
        def search(self,code): return [pick]
        def order(self,*a,**kw): calls.append(a);return OrderResult(success=True)
    outcome=run_fallback([({'insurance_code':'694003321','unit':'12EA','quantity':15},15)],['복산','백제'],lambda s:Crawler(),lambda s:'nolock',lambda *a:True,lambda *a:None,rec.pending,rec.result,rec.unconfirmed)[0]
    assert outcome.state=='unconfirmed' and len(calls)==1
    cur=origin.cursor()
    cur.execute("SELECT success,\"reasonCode\" FROM domae_cloud_orders WHERE id='f'")
    assert cur.fetchone()==(None,'fallback_pending')
    sch._fail_pending_rows(cur,'b','worker failed')
    origin.commit()
    cur.execute("SELECT success FROM domae_cloud_orders WHERE id='f'")
    assert cur.fetchone()==(None,)
    assert sch._finalize_if_confirmed(origin,cur,'b','retry')
