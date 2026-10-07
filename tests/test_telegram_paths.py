"""Actual callback methods against local PostgreSQL, only network boundaries replaced."""
import sys
sys.path.insert(0, 'src')
import fakeredis
from copy import deepcopy
import pytest
from domae_mcp.cloud import scheduler as sch
from domae_mcp.cloud import notifier as nmod
from domae_mcp.core.crawlers.base import OrderResult, SearchResult
from tests.urgent_db_fixture import urgent_database, seed_monitor


@pytest.fixture(params=['auto_order_retry', 'telegram_order'])
def env(request, urgent_database, monkeypatch):
    db = urgent_database
    mid = seed_monitor(db)
    pool = db.pool()
    scheduler = sch.CloudScheduler(pool, fakeredis.FakeRedis())
    calls, sent = [], []
    state = {'result': OrderResult(success=True)}
    class Crawler:
        def login(self, *a): return True
        def search(self, kw):
            return [SearchResult(maker='', product_name='씨투스', unit='12EA', insurance_code='123456789',
                quantity=9, supplier='티제이팜', price=700, product_id='P1')]
        def order(self, pid, qty, **kw):
            calls.append((pid, qty, kw))
            # The durable marker must be visible before the external send boundary.
            with db.connection() as conn, conn.cursor() as cur:
                cur.execute('SELECT success,"reasonCode" FROM domae_cloud_orders')
                rows = cur.fetchall()
                assert rows.count((None, 'send_unknown')) == 1 and len(rows) == len(calls)
            if isinstance(state['result'], BaseException): raise state['result']
            return state['result']
    scheduler._crawlers = {'티제이팜': Crawler, '백제': Crawler}
    scheduler._crawlers_loaded = True
    scheduler._decrypt_creds = lambda c: c
    scheduler._search_alternatives = lambda *a: [{'supplier': '백제', 'product_id': 'B1', 'price': 800}]
    class Response:
        status_code = 200
        def json(self): return {'ok': True, 'result': {'message_id': 77}}
    def post(*a, **kw):
        sent.append(kw['json'])
        if state.get('notify_fail'): raise RuntimeError('delivery failed')
        return Response()
    monkeypatch.setenv('DOMAE_TELEGRAM_BOT_TOKEN', 'local-test')
    monkeypatch.setattr(nmod.requests, 'post', post)
    nmod.Notifier.set_delivery_sinks()
    job = {'monitor_id': mid, 'monitor_prefix': mid[:8], 'supplier': '티제이팜', 'product_id': 'P1',
        'quantity': 5, 'chat_id': 'c', 'message_id': 77, 'original_text': '원문', 'tried_suppliers': []}
    class Env:
        def run(self, **over): getattr(scheduler, request.param)({**job, **over})
        def rows(self):
            with db.connection() as conn, conn.cursor() as cur:
                cur.execute('''SELECT success,"reasonCode","confirmedQuantity","attemptKey","orderId",
                    "adjustedQuantity","availableStock",unit,"insuranceCode","batchId" FROM domae_cloud_orders ORDER BY "orderedAt"''')
                return cur.fetchall()
    e = Env()
    e.db, e.pool, e.scheduler, e.calls, e.sent, e.state, e.mid, e.path, e.crawler = db, pool, scheduler, calls, sent, state, mid, request.param, Crawler
    yield e
    pool.closeall()


def buttons(env):
    return any(p.get('reply_markup', {}).get('inline_keyboard') for p in env.sent)


@pytest.mark.parametrize('result,expected,alternatives', [
    (OrderResult(success=False, reason_code='send_unknown', fulfilled_quantity=2), (None,'send_unknown',2),False),
    (TimeoutError('lost'), (None,'send_unknown',None),False),
    (OrderResult(success=False), (None,'send_unknown',None),False),
    (OrderResult(success=False, reason_code='other'), (None,'send_unknown',None),False),
    (None, (None,'send_unknown',None),False),
    ({'success': True}, (None,'send_unknown',None),False),
    (OrderResult(success=False, reason_code='not_sent'), (False,'not_sent',None),True),
    (OrderResult(success=False, reason_code='stock_zero'), (False,'stock_zero',None),True),
    (OrderResult(success=False, reason_code='rejected'), (False,'rejected',None),False),
    (OrderResult(success=False, reason_code='isolated_fail'), (False,'isolated_fail',None),False),
    (OrderResult(success=False, reason_code='different_failure'), (False,'different_failure',None),False),
    (OrderResult(success=True, reason_code='stock_adjusted', adjusted_quantity=3, order_id='O1', available_stock=3), (True,'stock_adjusted',3),False),
    (OrderResult(success=True), (True,None,5),False),
])
def test_outcomes_and_transport_markup(env, result, expected, alternatives):
    env.state['result'] = result
    env.run()
    row = env.rows()[0]
    assert row[:3] == expected
    assert len(env.calls) == 1 and buttons(env) == alternatives
    assert row[3] == f"{'ao' if env.path == 'auto_order_retry' else 'tg'}:{env.mid}:c:77:티제이팜:P1"
    edits = [p for p in env.sent if 'message_id' in p]
    assert edits
    if not alternatives: assert edits[-1]['reply_markup'] == {'inline_keyboard': []}
    if expected[0] is None: assert '전송 결과 확인 필요' in edits[-1]['text']
    if expected[0] and expected[2] == 3:
        assert row[4:7] == ('O1',3,3) and '3' in edits[-1]['text']
    if env.path == 'telegram_order':
        assert row[7:9] == ('12EA','123456789') and row[9]


def test_replay_after_redis_flush_orders_once(env):
    env.run(); env.scheduler._redis.flushall(); env.run()
    assert len(env.calls) == 1 and len(env.rows()) == 1


def test_crash_after_pending_commit_blocks_replay(env):
    env.state['result'] = SystemExit('crash')
    with pytest.raises(SystemExit): env.run()
    env.state['result'] = OrderResult(success=True)
    env.run()
    assert len(env.calls) == 1 and env.rows()[0][:3] == (None,'send_unknown',None)


def test_notification_failure_preserves_order(env):
    env.state['notify_fail'] = True
    env.run(); env.run()
    assert len(env.calls) == 1 and env.rows()[0][:3] == (True,None,5)


@pytest.mark.parametrize('boundary', ['pending_insert', 'pending_commit', 'pending_commit_ambiguity', 'result_update', 'result_commit', 'result_commit_ambiguity'])
def test_database_failure_never_resends(env, boundary):
    # Inject SQL/commit faults around actual PG connections; all other SQL stays real.
    pool = env.pool
    class Cursor:
        def __init__(self, cur): self.cur = cur
        def execute(self, sql, params=None):
            if boundary == 'pending_insert' and 'INSERT INTO domae_cloud_orders' in sql:
                self.cur.execute('SELECT 1/0')
            if boundary == 'result_update' and 'UPDATE domae_cloud_orders' in sql:
                self.cur.execute('SELECT 1/0')
            return self.cur.execute(sql, params)
        def __getattr__(self, key): return getattr(self.cur,key)
    class Connection:
        def __init__(self, conn): self.conn, self.commits = conn, 0
        def cursor(self): return Cursor(self.conn.cursor())
        def commit(self):
            self.commits += 1
            if boundary == 'pending_commit' or (boundary == 'result_commit' and self.commits == 2):
                raise RuntimeError('commit failed')
            self.conn.commit()
            if boundary == 'pending_commit_ambiguity' or (boundary == 'result_commit_ambiguity' and self.commits == 2):
                raise RuntimeError('commit acknowledgement lost')
        def __getattr__(self,key): return getattr(self.conn,key)
    class Pool:
        def getconn(self): return Connection(pool.getconn())
        def putconn(self, conn, **kw):
            conn.rollback(); pool.putconn(conn.conn, **kw)
    env.scheduler._db_pool = Pool()
    env.run(); env.run()
    if boundary in ('pending_insert', 'pending_commit'):
        assert not env.calls and not env.rows()
    elif boundary == 'pending_commit_ambiguity':
        assert not env.calls and env.rows()[0][:3] == (None,'send_unknown',None)
    elif boundary == 'result_commit_ambiguity':
        assert len(env.calls) == 1 and env.rows()[0][:3] == (True,None,5)
    else:
        assert len(env.calls) == 1 and env.rows()[0][:3] == (None,'send_unknown',None)


@pytest.mark.parametrize('adjusted', [0, True, False, 0.0, 1.5, -1, 6, '3'])
def test_success_uses_only_validated_quantity(env, adjusted):
    env.state['result'] = OrderResult(success=True, adjusted_quantity=adjusted)
    env.run()
    row = env.rows()[0]
    if type(adjusted) is int and adjusted == 0:
        assert row[:3] == (True, None, 0) and row[5] == 0
        assert '0개' in env.sent[-1]['text']
    else:
        assert row[:3] == (None,'send_unknown',None) and not buttons(env)
        assert row[5] is None


@pytest.mark.parametrize('success', [False, True])
@pytest.mark.parametrize('adjusted', [1.5, 6])
def test_bad_adjustment_preserves_independent_partial_evidence(env, success, adjusted):
    env.state['result'] = OrderResult(success=success, reason_code='stock_adjusted' if success else 'not_sent',
        adjusted_quantity=adjusted, fulfilled_quantity=2)
    env.run()
    row = env.rows()[0]
    assert row[:3] == (None, 'send_unknown', 2) and row[5] is None
    assert '확정 수량 2개' in env.sent[-1]['text']
    assert len(env.calls) == 1 and not buttons(env)


@pytest.mark.parametrize('reason', ['not_sent', 'stock_zero', 'rejected'])
@pytest.mark.parametrize('adjusted,fulfilled', [(3, 0), (3, 2), (3, 4), (None, 3)])
def test_failed_adjustment_receipt_blocks_telegram_alternatives(env, monkeypatch, reason, adjusted, fulfilled):
    searches = []
    monkeypatch.setattr(env.scheduler, '_search_alternatives', lambda *a: searches.append(a) or [])
    env.state['result'] = OrderResult(reason_code=reason, adjusted_quantity=adjusted, fulfilled_quantity=fulfilled)
    env.run()
    assert env.rows()[0][:3] == (None, 'send_unknown', max(3, fulfilled))
    assert not searches and not buttons(env) and len(env.calls) == 1
    assert f'확정 수량 {max(3, fulfilled)}개' in env.sent[-1]['text']


def test_distinct_message_ids_and_path_prefixes_are_separate(env):
    # Each call sees its own newly committed marker, alongside previous results.
    env.run()
    env.run(message_id=78)
    other = 'telegram_order' if env.path == 'auto_order_retry' else 'auto_order_retry'
    getattr(env.scheduler, other)({'monitor_id':env.mid,'monitor_prefix':env.mid[:8],
        'supplier':'티제이팜','product_id':'P1','quantity':5,'chat_id':'c',
        'message_id':77,'original_text':'원문'})
    assert len(env.calls) == 3 and len({row[3] for row in env.rows()}) == 3


@pytest.mark.parametrize('dsn', [
    'postgresql://u:p@production.example/domae_resilience',
    'postgresql://u:p@127.0.0.1/pharmsquare',
    'host=localhost hostaddr=192.0.2.1 dbname=domae_resilience',
])
def test_fixture_rejects_unsafe_targets(dsn):
    from tests.urgent_db_fixture import validate_test_dsn
    with pytest.raises(ValueError): validate_test_dsn(dsn)


def test_callback_transport_escapes_original_text(env):
    env.run(original_text='<unsafe>&')
    assert '&lt;unsafe&gt;&amp;' in env.sent[-1]['text']


def test_success_keeps_order_amount(env):
    env.run()
    assert '3,500원' in env.sent[-1]['text']


def test_concurrent_callbacks_share_one_durable_attempt(env, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    entered, release = Event(), Event()
    order = env.crawler.order
    def blocked(*a, **kw):
        entered.set()
        assert release.wait(5)
        return order(*a, **kw)
    monkeypatch.setattr(env.crawler, 'order', blocked)
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(env.run)
        assert entered.wait(5)
        second = executor.submit(env.run)
        try: second.result(timeout=5)
        finally: release.set()
        first.result(timeout=5)
    assert len(env.calls) == 1 and len(env.rows()) == 1


def test_unknown_reason_never_claims_full_success_quantity(env):
    env.state['result'] = OrderResult(success=True, reason_code='send_unknown', fulfilled_quantity=2)
    env.run()
    assert env.rows()[0][:3] == (None,'send_unknown',2)
    assert '확정 수량 2개' in env.sent[-1]['text'] and not buttons(env)


@pytest.mark.parametrize('env', ['telegram_order'], indirect=True)
def test_telegram_preserves_inventory_snapshot_metadata(env):
    with env.db.connection() as conn, conn.cursor() as cur:
        cur.execute('''INSERT INTO domae_inventory_snapshots
            (id,"monitorId",supplier,"productName",unit,"insuranceCode",price,"productId")
            VALUES ('snapshot',%s,'티제이팜','씨투스 스냅샷','30EA','999999999',800,'P1')''', (env.mid,))
    env.run()
    assert env.rows()[0][7:9] == ('30EA','999999999')
    assert env.calls[0][2]['insurance_code'] == '999999999'


@pytest.mark.parametrize('boundary', ['result_update', 'result_commit'])
@pytest.mark.parametrize('result,known', [
    (OrderResult(success=False, reason_code='send_unknown', fulfilled_quantity=2), 2),
    (OrderResult(success=True, reason_code='stock_adjusted', adjusted_quantity=3), 3),
    (OrderResult(success=True, reason_code='stock_adjusted', adjusted_quantity=0), 0),
])
def test_result_record_failure_keeps_known_quantity_in_notice(env, boundary, result, known):
    pool = env.pool
    class Cursor:
        def __init__(self, cur): self.cur = cur
        def execute(self, sql, params=None):
            if boundary == 'result_update' and 'UPDATE domae_cloud_orders' in sql:
                self.cur.execute('SELECT 1/0')
            return self.cur.execute(sql, params)
        def __getattr__(self, name): return getattr(self.cur, name)
    class Connection:
        def __init__(self, conn): self.conn, self.commits = conn, 0
        def cursor(self): return Cursor(self.conn.cursor())
        def commit(self):
            self.commits += 1
            if boundary == 'result_commit' and self.commits == 2:
                raise RuntimeError('result commit failed')
            self.conn.commit()
        def __getattr__(self, name): return getattr(self.conn, name)
    class Pool:
        def getconn(self): return Connection(pool.getconn())
        def putconn(self, conn, **kw):
            conn.rollback()
            pool.putconn(conn.conn, **kw)
    env.scheduler._db_pool = Pool()
    env.state['result'] = deepcopy(result)
    env.run()
    assert env.rows()[0][:3] == (None, 'send_unknown', None)
    assert f'확정 수량 {known}개' in env.sent[-1]['text']
    assert '전송 결과 확인 필요' in env.sent[-1]['text']
    assert env.sent[-1]['reply_markup'] == {'inline_keyboard': []}
    env.run()
    assert len(env.calls) == 1 and not buttons(env)


@pytest.mark.parametrize('reason', ['not_sent', 'stock_zero', 'rejected', 'isolated_fail'])
@pytest.mark.parametrize('fulfilled,known', [(2, 2), (True, None), (6, None), (-1, None), ('2', None), (None, None)])
def test_failed_fulfillment_evidence_blocks_alternatives(env, monkeypatch, reason, fulfilled, known):
    searches = []
    monkeypatch.setattr(env.scheduler, '_search_alternatives', lambda *a: searches.append(a) or [])
    env.state['result'] = OrderResult(success=False, reason_code=reason, fulfilled_quantity=fulfilled)
    env.run()
    assert env.rows()[0][:3] == (None, 'send_unknown', known)
    assert '전송 결과 확인 필요' in env.sent[-1]['text']
    if known is not None:
        assert f'확정 수량 {known}개' in env.sent[-1]['text']
    else:
        assert '확정 수량' not in env.sent[-1]['text']
    assert env.sent[-1]['reply_markup'] == {'inline_keyboard': []}
    assert not searches and not buttons(env)
    env.run()
    assert len(env.calls) == 1 and not searches


def test_partial_callback_order_names_the_remainder(env):
    r = OrderResult(success=True, reason_code='stock_adjusted', adjusted_quantity=3, fulfilled_quantity=3,
                    message='3개 주문 — 남은 2개는 확인 실패로 주문 안 함')
    r.shortfall_reason = 'stopped'
    env.state['result'] = r
    env.run()
    text = env.sent[-1]['text']
    assert '3개 주문 완료 (요청 5개, 남은 2개는 확인 실패로 주문 안 함 — 남은 수량은 다시 주문해야 합니다)' in text
    assert '재고 부족' not in text


def test_partial_callback_order_stock_shortage(env):
    env.state['result'] = OrderResult(success=True, reason_code='stock_adjusted', adjusted_quantity=3)
    env.run()
    assert '3개 주문 완료 (요청 5개, 부족 2개는 재고 부족 — 남은 수량은 다시 주문해야 합니다)' in env.sent[-1]['text']
