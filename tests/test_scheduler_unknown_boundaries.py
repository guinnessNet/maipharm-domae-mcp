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


def _stopped(done=10, detail='남은 5개는 확인 실패로 주문 안 함'):
    r = OrderResult(success=True, reason_code='stock_adjusted', adjusted_quantity=done, fulfilled_quantity=done,
                    message=f'{done}개 주문 — {detail}')
    r.shortfall_reason = 'stopped'
    return r


def test_quick_order_payload_carries_shortfall_reason_and_detail(env):
    run_reported_result(env, 'order', _stopped())
    payload = json.loads(env.scheduler._redis.rpop('response'))
    assert payload['shortfall_reason'] == 'stopped' and payload['available_stock'] is None
    assert payload['message'] == '10개 주문 — 남은 5개는 확인 실패로 주문 안 함'
    assert env.read()[0] == (True, 'stock_adjusted', 10, 10)
    assert any('요청 15개, 남은 5개는 확인 실패로 주문 안 함' in str(n) for n in env.notices)
    assert not any('재고 부족' in str(n) for n in env.notices)


def test_auto_order_stopped_remainder_stays_in_cart_without_fallback(env):
    published = []
    env.scheduler._redis.publish = lambda ch, msg: published.append(json.loads(msg))
    run_reported_result(env, 'auto_order', _stopped())
    order, cart, batch = env.read()
    assert env.alternatives == []                          # 재고 외 사유 미주문분은 대체주문하지 않는다
    assert cart[0] == 5 and '확인 실패로 주문 안 함' in cart[1] and '재고 부족' not in cart[1]
    text = ' '.join(str(n) for n in env.notices)
    assert '남은 5개는 확인 실패로 주문 안 함, 장바구니에 남김' in text and '부족 5개' not in text
    sse = [p for p in published if p.get('type') == 'auto_order_result'][-1]
    assert sse['unsent'] == 5 and sse['shortfall'] == 0


def test_auto_order_stock_shortage_remainder_still_falls_back(env):
    run_reported_result(env, 'auto_order', OrderResult(success=True, reason_code='stock_adjusted', adjusted_quantity=10))
    assert 'search' in env.alternatives                    # 재고 부족분은 다음 순번 도매에서 찾는다


def test_auto_order_mixed_stopped_keeps_all_in_cart_and_splits_counts(env):
    published = []
    env.scheduler._redis.publish = lambda ch, msg: published.append(json.loads(msg))
    r = _stopped(detail='남은 3개는 확인 실패로 주문 안 함, 2개는 재고 부족')
    r.unsent_quantity = 3
    run_reported_result(env, 'auto_order', r)
    order, cart, batch = env.read()
    assert env.alternatives == [] and cart[0] == 5
    assert '남은 3개는 확인 실패로 주문 안 함, 2개는 재고 부족' in cart[1]
    sse = [p for p in published if p.get('type') == 'auto_order_result'][-1]
    assert sse['unsent'] == 3 and sse['shortfall'] == 2


@pytest.mark.parametrize('filled', [5, 3])
def test_auto_order_fallback_filled_shortage_is_not_reported_as_left_in_cart(env, monkeypatch, filled):
    from domae_mcp.cloud.fallback import FallbackOutcome
    published = []
    env.scheduler._redis.publish = lambda ch, msg: published.append(json.loads(msg))
    monkeypatch.setattr(sch, 'run_fallback', lambda needs, *a, **k: [
        FallbackOutcome(item, need, '백제', filled, 'ordered', '') for item, need in needs])
    run_reported_result(env, 'auto_order', OrderResult(success=True, reason_code='stock_adjusted', adjusted_quantity=10))
    sse = [p for p in published if p.get('type') == 'auto_order_result'][-1]
    text = ' '.join(str(n) for n in env.notices)
    assert sse['fallbackOrdered'] == 1 and sse['shortfall'] == 5 - filled and sse['unsent'] == 0
    if filled == 5:
        assert sse['status'] == 'success'
        assert '부족 5개 중 백제에 5개 대체주문)' in text and '장바구니에 남김' not in text
        assert env.read()[1] is None                             # 장바구니 행 삭제
    else:
        assert sse['status'] == 'partial_fail'
        assert '부족 5개 중 백제에 3개 대체주문, 2개는 장바구니에 남김)' in text
        assert env.read()[1][0] == 2


def test_auto_order_stock_zero_fully_filled_by_fallback_reports_success(env, monkeypatch):
    from domae_mcp.cloud.fallback import FallbackOutcome
    published = []
    env.scheduler._redis.publish = lambda ch, msg: published.append(json.loads(msg))
    monkeypatch.setattr(sch, 'run_fallback', lambda needs, *a, **k: [
        FallbackOutcome(item, need, '백제', need, 'ordered', '') for item, need in needs])
    run_reported_result(env, 'auto_order', OrderResult(reason_code='stock_zero', adjusted_quantity=0))
    sse = [p for p in published if p.get('type') == 'auto_order_result'][-1]
    text = ' '.join(str(n) for n in env.notices)
    assert sse['status'] == 'success' and sse['fallbackOrdered'] == 1
    assert '자동주문 실패' not in text and '백제 15개 주문 완료' in text
    assert env.read()[1] is None


def test_auto_order_unconfirmed_fallback_is_not_reported_as_left_in_cart(env, monkeypatch):
    from domae_mcp.cloud.fallback import FallbackOutcome
    published = []
    env.scheduler._redis.publish = lambda ch, msg: published.append(json.loads(msg))
    monkeypatch.setattr(sch, 'run_fallback', lambda needs, *a, **k: [
        FallbackOutcome(item, need, '백제', 0, 'unconfirmed', '백제 주문 결과 불명 — 확인 필요') for item, need in needs])
    run_reported_result(env, 'auto_order', OrderResult(success=True, reason_code='stock_adjusted', adjusted_quantity=10))
    sse = [p for p in published if p.get('type') == 'auto_order_result'][-1]
    text = ' '.join(str(n) for n in env.notices)
    assert sse['status'] == 'unconfirmed' and sse['shortfall'] == 0 and sse['fallbackUnconfirmed'] == 1
    assert '백제 대체주문 5개 결과 확인 필요 — 도매몰 주문내역 확인 전 재주문 금지' in text
    assert '장바구니에 남김' not in text and '자동주문 부분 완료' not in text


def test_auto_order_fallback_exception_keeps_accepted_fallback_and_cart(env, monkeypatch):
    """대체주문 도중 예외가 나도 이미 접수된 대체주문 결과는 장바구니·알림에 반영된다(실패·재주문 버튼 금지)."""
    from domae_mcp.cloud.fallback import FallbackOutcome
    def boom(needs, *a, outcomes=None, **k):
        item, need = needs[0]
        outcomes.append(FallbackOutcome(item, need, '백제', need, 'ordered', ''))
        raise ConnectionError('redis down')
    monkeypatch.setattr(sch, 'run_fallback', boom)
    run_reported_result(env, 'auto_order', OrderResult(reason_code='stock_zero', adjusted_quantity=0))
    text = ' '.join(str(n) for n in env.notices)
    assert env.read()[1] is None                               # 접수분 반영 → 장바구니 행 삭제
    assert '자동주문 실패' not in text and '백제 15개 주문 완료' in text and "'inline_keyboard'" not in text


def test_auto_order_blocked_fallback_sse_is_not_unconfirmed(env, monkeypatch):
    from domae_mcp.cloud.fallback import FallbackOutcome
    published = []
    env.scheduler._redis.publish = lambda ch, msg: published.append(json.loads(msg))
    monkeypatch.setattr(sch, 'run_fallback', lambda needs, *a, outcomes=None, **k: outcomes.extend(
        FallbackOutcome(item, need, '백제', 0, 'blocked', '') for item, need in needs) or outcomes)
    run_reported_result(env, 'auto_order', OrderResult(success=True, reason_code='stock_adjusted', adjusted_quantity=10))
    sse = [p for p in published if p.get('type') == 'auto_order_result'][-1]
    assert sse['fallbackUnconfirmed'] == 0 and sse['fallbackBlocked'] == 1 and sse['fallbackBlockedQty'] == 5


def test_auto_order_midloop_exception_reports_unprocessed_sent_items_as_check(env, monkeypatch):
    """전송 뒤 결과 기록 중 예외: 남은 품목은 '전송 결과 확인 필요', 제목은 완료가 아니다."""
    calls = {'n': 0}
    original = sch._record_order_result
    def flaky(*a, **k):
        calls['n'] += 1
        if calls['n'] == 1:
            raise RuntimeError('db down')
        return original(*a, **k)
    monkeypatch.setattr(sch, '_record_order_result', flaky)
    run_reported_result(env, 'auto_order', OrderResult(success=True))
    text = ' '.join(str(n) for n in env.notices)
    assert '✅ 자동주문 완료' not in text
    assert '전송 결과 확인 필요' in text and '약품 15개' in text
