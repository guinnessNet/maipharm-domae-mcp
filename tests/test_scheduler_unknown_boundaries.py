"""Actual scheduler result ingestion against scoped local PG; only supplier/notification I/O replaced."""
import sys
sys.path.insert(0, 'src')
import json
import fakeredis
import pytest
from domae_mcp.cloud import scheduler as sch
from domae_mcp.core.crawlers.base import OrderResult
from tests.urgent_db_fixture import urgent_database, seed_monitor


@pytest.fixture
def env(urgent_database, monkeypatch):
    db = urgent_database
    mid = seed_monitor(db, ('인천', '백제'))
    # Reuse the isolated schema; these two additional tables match their actual Prisma fields.
    with db.connection() as conn, conn.cursor() as cur:
        cur.execute('''CREATE TABLE domae_cart_items (
            id text PRIMARY KEY,"monitorId" text NOT NULL REFERENCES domae_cloud_monitors(id),
            "insuranceCode" text,"productName" text NOT NULL,unit text,quantity integer NOT NULL,
            price integer,supplier text,"productId" text,"failedAt" timestamp,"failReason" text,
            "syncStatus" text,"syncError" text,"syncedAt" timestamp,
            "createdAt" timestamp NOT NULL DEFAULT now(),"updatedAt" timestamp NOT NULL DEFAULT now(),
            UNIQUE ("monitorId","productId",supplier));
            CREATE TABLE domae_auto_order_logs (
            id text PRIMARY KEY,"monitorId" text NOT NULL REFERENCES domae_cloud_monitors(id),
            supplier text NOT NULL,"scheduledAt" text NOT NULL,"triggeredAt" timestamp NOT NULL,
            "batchId" text REFERENCES domae_order_batches(id),"itemCount" integer NOT NULL,
            status text NOT NULL,message text,"createdAt" timestamp NOT NULL DEFAULT now());''')
        cur.execute('UPDATE domae_cloud_monitors SET "autoFallbackOrder"=true WHERE id=%s', (mid,))
        cur.execute('INSERT INTO domae_order_batches (id,"monitorId","totalItems") VALUES (%s,%s,1)', ('batch', mid))
        cur.execute('''INSERT INTO domae_cloud_orders
            (id,"monitorId","batchId",supplier,"productName",quantity,"productId")
            VALUES ('order',%s,'batch','인천','약품',15,'P')''', (mid,))
        cur.execute('''INSERT INTO domae_cart_items
            (id,"monitorId",supplier,"productName",quantity,"productId") VALUES ('cart',%s,'인천','약품',15,'P')''', (mid,))
        cur.execute('''INSERT INTO domae_auto_order_logs
            (id,"monitorId",supplier,"scheduledAt","triggeredAt","batchId","itemCount",status)
            VALUES ('auto',%s,'인천','12:00',now(),'batch',1,'pending')''', (mid,))
    pool = db.pool()
    scheduler = sch.CloudScheduler(pool, fakeredis.FakeRedis())
    scheduler._crawlers_loaded = True
    calls, alternatives, notices = [], [], []
    state = {'first': OrderResult(success=True), 'retry': OrderResult(success=True)}
    def marker():
        with db.connection() as conn, conn.cursor() as cur:
            cur.execute('SELECT success,"reasonCode" FROM domae_cloud_orders WHERE id=\'order\'')
            assert cur.fetchone() == (None, 'send_unknown')
    class Crawler:
        def login(self, *args): return True
        def search(self, keyword): return []
        def order_batch(self, items):
            marker(); calls.append(('batch', items[0]['quantity']))
            return [state['first']]
        def order(self, pid, quantity, **kwargs):
            marker(); calls.append(('order', quantity))
            return state['retry'] if state.get('retrying') else state['first']
    class Alternative:
        def login(self, *args): alternatives.append('login'); return True
        def search(self, keyword): alternatives.append('search'); return []
        def order(self, *args, **kwargs): alternatives.append('order'); return OrderResult(success=True)
    scheduler._crawlers = {'인천': Crawler, '백제': Alternative}
    monkeypatch.setattr(sch.time, 'sleep', lambda *_: None)
    monkeypatch.setattr(sch.Notifier, 'send_telegram', lambda *args, **kwargs: notices.append((args, kwargs)))
    item = {'supplier': '인천', 'product_id': 'P', 'product_name': '약품', 'quantity': 15,
            'insurance_code': '123456789', 'unit': '12EA', 'price': 700,
            'db_order_id': 'order', 'cart_item_id': 'cart'}
    class Env:
        def run(self, path):
            if path == 'order':
                scheduler.order({**item, 'monitor_id': mid, 'response_key': 'response', 'db_batch_id': 'batch'})
            else:
                getattr(scheduler, path)({'monitor_id': mid, 'batch_id': 'batch', 'supplier': '인천', 'items': [item]})
        def read(self):
            with db.connection() as conn, conn.cursor() as cur:
                cur.execute('SELECT success,"reasonCode","confirmedQuantity","adjustedQuantity" FROM domae_cloud_orders WHERE id=\'order\'')
                order = cur.fetchone()
                cur.execute('SELECT quantity,"failReason" FROM domae_cart_items WHERE id=\'cart\'')
                cart = cur.fetchone()
                cur.execute('SELECT status,"failCount" FROM domae_order_batches WHERE id=\'batch\'')
                return order, cart, cur.fetchone()
    e = Env()
    e.state, e.calls, e.alternatives, e.notices, e.scheduler, e.pool = state, calls, alternatives, notices, scheduler, pool
    yield e
    pool.closeall()


@pytest.mark.parametrize('path', ['auto_order', 'batch_order', 'order'])
@pytest.mark.parametrize('reason', [None, 'other'])
@pytest.mark.parametrize('fulfilled', [0, 3])
def test_unspecified_failure_is_unknown_at_actual_scheduler_boundary(env, path, reason, fulfilled):
    env.state['first'] = OrderResult(success=False, reason_code=reason, fulfilled_quantity=fulfilled)
    env.run(path)
    order, cart, batch = env.read()
    assert order[:3] == (None, 'send_unknown', fulfilled or None)
    assert cart[0] == 15 and '전송 결과 확인 필요' in cart[1]
    assert batch == ('processing', 0)
    assert env.calls == [('order' if path == 'order' else 'batch', 15)]
    assert not env.alternatives
    assert any('확인 필요' in str(notice) for notice in env.notices)
    assert not any(notice[1].get('reply_markup', {}).get('inline_keyboard') for notice in env.notices if notice[1].get('reply_markup'))
    if path == 'order':
        response = json.loads(env.scheduler._redis.lpop('response'))
        assert response['success'] is None and response['reason_code'] == 'send_unknown'
    assert not env.pool._used


@pytest.mark.parametrize('reason', [None, 'other'])
@pytest.mark.parametrize('fulfilled', [0, 3])
def test_safe_first_failure_then_unspecified_direct_retry_remains_unknown(env, reason, fulfilled):
    env.state.update(first=OrderResult(success=False, reason_code='not_sent'),
        retry=OrderResult(success=False, reason_code=reason, fulfilled_quantity=fulfilled), retrying=True)
    env.run('batch_order')
    order, cart, batch = env.read()
    assert order[:3] == (None, 'send_unknown', fulfilled or None)
    assert env.calls == [('batch', 15), ('order', 15)] and not env.alternatives
    assert cart[0] == 15 and '전송 결과 확인 필요' in cart[1]
    assert batch == ('processing', 0) and any('확인 필요' in str(n) for n in env.notices)


def test_successful_retry_preserves_reported_adjusted_quantity(env):
    env.state.update(first=OrderResult(success=False, reason_code='not_sent'),
        retry=OrderResult(success=True, reason_code='stock_adjusted', adjusted_quantity=10), retrying=True)
    env.run('batch_order')
    order, cart, batch = env.read()
    assert order == (True, 'stock_adjusted', 10, 10)
    assert cart[0] == 5 and env.calls == [('batch', 15), ('order', 15)]
    assert any('주문 10' in str(n) for n in env.notices)


@pytest.mark.parametrize('reason,fulfilled,no_retry,retries', [
    ('not_sent', 0, True, False), ('not_sent', 3, False, False),
    ('rejected', 0, False, True), ('rejected', 0, True, False),
])
def test_explicit_safe_policy_is_unchanged(env, reason, fulfilled, no_retry, retries):
    env.state.update(first=OrderResult(success=False, reason_code=reason,
        fulfilled_quantity=fulfilled, no_retry=no_retry), retrying=True)
    env.run('batch_order')
    assert len(env.calls) == (2 if retries else 1)
    assert env.read()[0][1] == ('ok' if retries else 'send_unknown' if fulfilled > 0 else reason)


@pytest.mark.parametrize('path', ['batch_order', 'auto_order', 'order'])
@pytest.mark.parametrize('reason', ['not_sent', 'rejected'])
@pytest.mark.parametrize('fulfilled', [False, 0.0, None, '0', -1, 16])
def test_invalid_fulfillment_is_protected_without_any_resend(env, path, reason, fulfilled):
    env.state.update(first=OrderResult(success=False, reason_code=reason,
        fulfilled_quantity=fulfilled), retrying=path == 'batch_order')
    env.run(path)
    order, cart, batch = env.read()
    assert env.calls == [('order' if path == 'order' else 'batch', 15)]
    assert not env.alternatives
    assert order[:3] == (None, 'send_unknown', None)
    assert cart[0] == 15 and '전송 결과 확인 필요' in cart[1]
    assert batch == ('processing', 0)
    assert any('확인 필요' in str(n) for n in env.notices)
    assert env.state['first'].fulfilled_quantity is fulfilled
    assert not env.pool._used


ADJUSTMENT_CASES = [(True, value) for value in (True, False, 0, 0.0, 1.5, 16, -1, '2')]
ADJUSTMENT_CASES += [(False, value) for value in (True, False, 0.0, 1.5, 16, -1, '2')]


def run_reported_result(env, path, result):
    retry = path == 'batch_retry'
    env.state.update(first=OrderResult(reason_code='not_sent') if retry else result,
        retry=result, retrying=retry)
    env.run('batch_order' if retry else path)


@pytest.mark.parametrize('path', ['batch_order', 'batch_retry', 'auto_order', 'order'])
@pytest.mark.parametrize('success,adjusted', ADJUSTMENT_CASES)
@pytest.mark.parametrize('fulfilled', [0, 3])
def test_invalid_adjustment_is_quarantined_before_sql_and_cart(env, path, success, adjusted, fulfilled):
    result = OrderResult(success=success, reason_code='stock_adjusted' if success else 'not_sent',
        adjusted_quantity=adjusted, fulfilled_quantity=fulfilled)
    run_reported_result(env, path, result)
    order, cart, batch = env.read()
    assert order == (None, 'send_unknown', fulfilled or None, None)
    assert cart[0] == 15 and '전송 결과 확인 필요' in cart[1]
    assert batch == ('processing', 0)
    expected = [('batch', 15), ('order', 15)] if path == 'batch_retry' else [
        ('order' if path == 'order' else 'batch', 15)]
    assert env.calls == expected and not env.alternatives
    assert result.fulfilled_quantity == fulfilled
    assert any('확인 필요' in str(n) for n in env.notices)
    assert not env.pool._used


@pytest.mark.parametrize('path', ['batch_order', 'batch_retry', 'auto_order', 'order'])
def test_valid_adjustment_still_records_actual_quantity(env, path):
    run_reported_result(env, path, OrderResult(success=True, reason_code='stock_adjusted', adjusted_quantity=10))
    assert env.read()[0] == (True, 'stock_adjusted', 10, 10)
    assert env.read()[1][0] == (15 if path == 'order' else 5)


@pytest.mark.parametrize('path', ['batch_order', 'batch_retry', 'auto_order', 'order'])
def test_confirmed_unsent_stock_zero_adjustment_remains_zero(env, path):
    run_reported_result(env, path, OrderResult(reason_code='stock_zero', adjusted_quantity=0))
    assert env.read()[0] == (False, 'stock_zero', None, 0)


@pytest.mark.parametrize('path', ['batch_order', 'batch_retry', 'auto_order', 'order'])
@pytest.mark.parametrize('reason', ['not_sent', 'stock_zero', 'rejected'])
@pytest.mark.parametrize('adjusted,fulfilled', [(3, 0), (3, 2), (3, 4), (None, 3)])
def test_failed_positive_adjustment_preserves_receipt_without_resend(env, path, reason, adjusted, fulfilled):
    result = OrderResult(reason_code=reason, adjusted_quantity=adjusted, fulfilled_quantity=fulfilled)
    run_reported_result(env, path, result)
    order, cart, batch = env.read()
    assert order == (None, 'send_unknown', max(adjusted or 0, fulfilled), adjusted)
    assert cart[0] == 15 and '전송 결과 확인 필요' in cart[1]
    assert batch == ('processing', 0)
    expected = [('batch', 15), ('order', 15)] if path == 'batch_retry' else [
        ('order' if path == 'order' else 'batch', 15)]
    assert env.calls == expected and not env.alternatives
    assert any('확인 필요' in str(n) for n in env.notices)
