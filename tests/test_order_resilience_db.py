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


def validate_test_dsn(dsn):
    options = psycopg2.extensions.parse_dsn(dsn)
    allowed = {'127.0.0.1', '::1', 'localhost'}
    if options.get('host') not in allowed or options.get('hostaddr', options.get('host')) not in allowed or options.get('dbname') != 'domae_resilience':
        raise ValueError('통합 테스트는 loopback의 전용 domae_resilience DB만 허용합니다')
    return dsn


@pytest.fixture
def database():
    dsn=os.environ.get('DOMAE_TEST_DATABASE_URL')
    if not dsn:
        pytest.skip('격리된 로컬 DB DSN을 지정해야 하는 통합 테스트')
    schema='resilience_'+uuid.uuid4().hex
    origin=psycopg2.connect(validate_test_dsn(dsn))
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
    c.execute("SELECT status,\"completedAt\" FROM domae_order_batches WHERE id='b'")
    assert c.fetchone()==('processing',None)


def test_only_unconfirmed_rows_block_rerun(database):
    origin,_=database
    cur=origin.cursor()
    cur.execute('''INSERT INTO domae_cloud_orders (id,"batchId",success,"reasonCode")
                   VALUES ('u','b',NULL,'send_unknown')''')
    origin.commit()
    assert sch._finalize_if_confirmed(origin,cur,'b','unknown only')
    cur.execute("SELECT success FROM domae_cloud_orders WHERE id='u'")
    assert cur.fetchone()==(None,)
    cur.execute("SELECT status,\"completedAt\" FROM domae_order_batches WHERE id='b'")
    assert cur.fetchone()==('processing',None)


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


@pytest.mark.parametrize('dsn',[
    'postgresql://u:p@production.example/domae_resilience',
    'postgresql://u:p@127.0.0.1/pharmsquare',
    'host=localhost hostaddr=192.0.2.1 dbname=domae_resilience',
])
def test_test_database_guard_rejects_unsafe_target(dsn):
    with pytest.raises(ValueError):
        validate_test_dsn(dsn)


@pytest.mark.parametrize('fallback_result,remaining,unknown',[
    (OrderResult(success=True),None,False),
    (OrderResult(success=True,reason_code='stock_adjusted',adjusted_quantity=3),2,False),
    (TimeoutError('response lost'),5,True),
    (SystemExit('worker killed after send boundary'),15,True),
])
def test_auto_order_to_real_fallback_recorder_and_cart(database,monkeypatch,fallback_result,remaining,unknown):
    import json
    from domae_mcp.cloud import notifier as nmod
    origin,secondary=database
    cur=origin.cursor()
    cur.execute('ALTER TABLE domae_order_batches ADD COLUMN "monitorId" text')
    cur.execute("UPDATE domae_order_batches SET status='pending',\"monitorId\"='m'")
    cur.execute('''CREATE TABLE domae_cloud_monitors (
        id text, credentials jsonb, "telegramChatId" text, "supplierOrder" jsonb,
        "autoFallbackOrder" boolean, "isActive" boolean)''')
    cur.execute('''CREATE TABLE domae_cart_items (
        id text, quantity integer, "failedAt" timestamp, "failReason" text)''')
    cur.execute('''CREATE TABLE domae_auto_order_logs (
        "monitorId" text, "batchId" text, status text, message text)''')
    creds={'인천':{'login_id':'test'},'복산':{'login_id':'test'}}
    cur.execute('INSERT INTO domae_cloud_monitors VALUES (%s,%s,%s,%s,true,true)',('m',json.dumps(creds),'chat',json.dumps(['인천','복산'])))
    cur.execute("INSERT INTO domae_cart_items VALUES ('c',15,NULL,NULL)")
    cur.execute("INSERT INTO domae_auto_order_logs VALUES ('m','b','pending',NULL)")
    cur.execute('''INSERT INTO domae_cloud_orders (id,"monitorId","batchId",supplier,
                   "productName",quantity,success) VALUES ('o','m','b','인천','베아놀',15,NULL)''')
    origin.commit()
    network_calls=[]
    class OriginCrawler:
        def login(self,*a): return True
        def search(self,*a): return []
        def order_batch(self,items):
            assert items[0]['insurance_code']=='694003321'
            if isinstance(fallback_result,SystemExit): raise fallback_result
            return [OrderResult(success=True,reason_code='stock_adjusted',adjusted_quantity=10)]
    class CandidateCrawler:
        def login(self,*a): return True
        def search(self,code):
            assert code=='694003321'
            return [SearchResult(maker='',product_name='wrong pack',unit='100EA',insurance_code=code,quantity=99,supplier='복산',price=900,product_id='wrong'),
                    SearchResult(maker='',product_name='베아놀',unit='12EA',insurance_code=code,quantity=8,supplier='복산',price=1000,product_id='candidate')]
        def order(self,pid,qty,**kw):
            # 외부 전송 경계에서 독립 DB 연결로 pending 커밋과 원 잔량 커밋을 읽는다.
            c=origin.cursor()
            c.execute("SELECT success,\"reasonCode\",quantity FROM domae_cloud_orders WHERE supplier='복산'")
            assert c.fetchone()==(None,'fallback_pending',5)
            c.execute("SELECT quantity,\"failedAt\" IS NOT NULL FROM domae_cart_items WHERE id='c'")
            assert c.fetchone()==(5,True)
            network_calls.append((pid,qty))
            if isinstance(fallback_result,Exception): raise fallback_result
            return fallback_result
    class Pool:
        def __init__(self): self.available=[origin,secondary];self.returned=[]
        def getconn(self): return self.available.pop(0)
        def putconn(self,conn): self.returned.append(conn)
    class Redis:
        def __init__(self): self.events=[]
        def publish(self,key,data): self.events.append(json.loads(data))
    sent=[]
    class Response:
        status_code=200
        def json(self): return {'ok':True,'result':{'message_id':1}}
    def send(*a,**kw): sent.append(kw['json']);return Response()
    monkeypatch.setenv('DOMAE_TELEGRAM_BOT_TOKEN','test:local')
    monkeypatch.setattr(nmod.requests,'post',send)
    nmod.Notifier.set_delivery_sinks()
    pool,redis=Pool(),Redis()
    scheduler=sch.CloudScheduler(pool,redis)
    scheduler._crawlers_loaded=True
    scheduler._crawlers={'인천':OriginCrawler,'복산':CandidateCrawler}
    item={'product_id':'original','product_name':'베아놀','quantity':15,'price':1000,'insurance_code':'694003321','unit':'12EA','db_order_id':'o','cart_item_id':'c'}
    job={'monitor_id':'m','batch_id':'b','supplier':'인천','items':[item]}
    if isinstance(fallback_result,SystemExit):
        with pytest.raises(SystemExit):
            scheduler.auto_order(job)
        cur.execute("SELECT success,\"reasonCode\" FROM domae_cloud_orders WHERE id='o'")
        assert cur.fetchone()==(None,'send_unknown')
        cur.execute("SELECT status,\"completedAt\" FROM domae_order_batches WHERE id='b'")
        assert cur.fetchone()==('processing',None)
        # 재수신해도 소유권을 얻지 못하므로 외부 주문으로 진행하지 않는다.
        pool.available.insert(0,origin)
        scheduler.auto_order(job)
        assert not network_calls
        return
    scheduler.auto_order(job)
    assert network_calls==[('candidate',5)] and pool.returned==[secondary,origin]
    cur.execute("SELECT success,\"reasonCode\",\"adjustedQuantity\" FROM domae_cloud_orders WHERE id='o'")
    assert cur.fetchone()==(True,'stock_adjusted',10)
    cur.execute("SELECT success,\"reasonCode\",quantity FROM domae_cloud_orders WHERE supplier='복산'")
    row=cur.fetchone()
    assert row[0] is (None if unknown else True) and row[2]==5
    if unknown: assert row[1]=='send_unknown'
    cur.execute("SELECT quantity,\"failedAt\" IS NOT NULL FROM domae_cart_items WHERE id='c'")
    assert cur.fetchone()==(None if remaining is None else (remaining,True))
    assert '요청 15개' in sent[0]['text'] and not sent[0].get('reply_markup')
    assert redis.events[0]['fallbackOrdered']==(0 if unknown else 1)
