"""검수 지적 A1·A5·C2 — 격리 PostgreSQL(domae_resilience)에서 실제 SQL 을 검증한다."""
import sys
sys.path.insert(0, 'src')
sys.path.insert(0, 'tests')
import pytest
from domae_mcp.cloud import scheduler as sch
from domae_mcp.cloud.fallback_db import FallbackRecorder
from test_order_resilience_db import database  # noqa: F401 (fixture)


def _cart(cur):
    cur.execute('CREATE TABLE domae_cart_items (id text, quantity integer, "failedAt" timestamp, "failReason" text)')
    cur.execute("INSERT INTO domae_cart_items VALUES ('c1',5,NULL,NULL),('c2',3,NULL,NULL),('other',1,NULL,NULL)")


def test_a1_cart_rows_committed_with_marker_and_survive_crash(database):
    origin, observer = database
    cur = origin.cursor()
    _cart(cur)
    cur.execute('''INSERT INTO domae_cloud_orders (id,"batchId",supplier,success) VALUES ('o','b','인천',NULL)''')
    origin.commit()
    sch._mark_sending(origin, cur, 'b', '인천', ['c1', 'c2'])
    # 전송 직후 예외 → 결과 기록 없이 rollback (워커 사망과 같은 상태)
    cur.execute("UPDATE domae_cart_items SET \"failedAt\" = NULL WHERE id = 'c1'")
    origin.rollback()
    c = observer.cursor()
    c.execute('SELECT id, "failedAt" IS NOT NULL, "failReason" FROM domae_cart_items ORDER BY id')
    assert c.fetchall() == [('c1', True, '전송 결과 확인 중'), ('c2', True, '전송 결과 확인 중'),
                            ('other', False, None)]
    c.execute("SELECT \"reasonCode\" FROM domae_cloud_orders WHERE id='o'")
    assert c.fetchone() == ('send_unknown',)


def test_a5_close_batch_keeps_processing_and_recounts(database):
    origin, observer = database
    cur = origin.cursor()
    cur.execute('''INSERT INTO domae_cloud_orders (id,"batchId",supplier,success,"reasonCode") VALUES
        ('a','b','인천',true,'ok'),('f','b','복산',true,'ok'),('z','b','인천',false,'stock_zero'),
        ('u','b','인천',NULL,'send_unknown')''')
    assert sch._close_batch(cur, 'b', 'completed', 1, 4) == (2, 1, 1)
    origin.commit()
    c = observer.cursor()
    c.execute('SELECT status,"completedAt","successCount","failCount","adjustedCount","missingQuantity" FROM domae_order_batches')
    assert c.fetchone() == ('processing', None, 2, 1, 1, 4)
    cur.execute("UPDATE domae_cloud_orders SET success=true WHERE id='u'")
    sch._close_batch(cur, 'b', 'completed')
    origin.commit()
    c.execute('SELECT status,"completedAt" IS NOT NULL,"successCount","failCount" FROM domae_order_batches')
    assert c.fetchone() == ('completed', True, 3, 1)


def test_c2_check_unconfirmed_sql(database):
    origin, secondary = database
    cur = origin.cursor()
    cur.execute('''INSERT INTO domae_cloud_orders (id,"monitorId","batchId",supplier,"productId",success) VALUES
        ('1','m','old','복산','P',NULL),('2','m','old','백제','Q',true),('3','other','x','백제','Q',NULL)''')
    origin.commit()
    rec = FallbackRecorder(secondary, 'm', 'b', '인천', None, None, None)
    assert rec.check_unconfirmed('복산', 'P') == 'same_product'
    assert rec.check_unconfirmed('복산', 'R') == 'other_product'
    assert rec.check_unconfirmed('백제', 'Q') is None     # 확정 행·다른 약국 행은 보지 않는다
