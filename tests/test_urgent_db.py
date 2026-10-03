"""C3 real scoped PostgreSQL ownership and durable receipt tests; no live supplier calls."""
import sys
import json
sys.path.insert(0, 'src')
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import fakeredis
import pytest
from domae_mcp.core.crawlers.base import OrderResult, SearchResult
from domae_mcp.cloud import scheduler as sch
from domae_mcp.cloud.notifier import Notifier
from tests.urgent_db_fixture import urgent_database, seed_monitor, seed_urgent, read_urgent


@pytest.fixture
def env(urgent_database, monkeypatch):
    database = urgent_database
    pool = database.pool()
    scheduler = sch.CloudScheduler(pool, fakeredis.FakeRedis())
    scheduler._crawlers_loaded = True
    alerts = []
    monkeypatch.delenv('DOMAE_URGENT_AUTO_ORDER', raising=False)
    monkeypatch.setattr(Notifier, 'send_telegram', lambda *a, **kw: alerts.append((a, kw)))
    monkeypatch.setattr(Notifier, 'send_urgent_order_result', lambda *a, **kw: alerts.append((a, kw)))
    yield scheduler, pool, database, alerts
    pool.closeall()


def make_crawler(stock=20, results=None, *, safe=True, login=True, before_search=None, on_order=None):
    receipts = list(results if results is not None else [OrderResult(success=True)])
    class C:
        orders, logins, instances = [], [], []
        def __init__(self):
            C.instances.append(self)
        def login(self, *args):
            C.logins.append(args)
            return login
        def search(self, keyword):
            assert C.logins, '검색 전 로그인 필수'
            assert callable(self.send_guard), '검색 중에도 guard 주입'
            self.send_guard()  # claim 이전 검색 단계에서는 no-op
            if before_search:
                before_search(self)
            return [SearchResult(product_name='씨투스', product_id=pid, quantity=stock, price=700)
                    for pid in ('P1', 'P2')]
        def order(self, pid, qty, **kw):
            self.send_guard()
            C.orders.append((pid, qty))
            if on_order:
                on_order(self)
            return receipts.pop(0)
    C.URGENT_ORDER_SAFE = safe
    return C


def setup(env, *, total=10, filled=0, suppliers=(('인천', 'P1'),), **cols):
    sc, pool, db, alerts = env
    names = tuple(dict.fromkeys(s for s, _ in suppliers))
    mid = seed_monitor(db, names)
    uo = seed_urgent(db, mid, total=total, filled=filled, suppliers=suppliers, **cols)
    creds = {s: {'login_id': 'local-test', 'login_pw': 'local-test'} for s in names}
    return uo, mid, creds


def run(env, uo, creds):
    sc, pool, db, alerts = env
    conn = pool.getconn()
    try:
        with conn.cursor() as cur:
            return sc._urgent_fill(conn, cur, uo, creds)
    finally:
        conn.rollback()
        pool.putconn(conn)


def update(db, uo, expression, params=()):
    with db.connection() as conn, conn.cursor() as cur:
        cur.execute('UPDATE domae_urgent_orders SET ' + expression + ' WHERE id=%s', (*params, uo))


def logs(db, uo):
    with db.connection() as conn, conn.cursor() as cur:
        cur.execute('SELECT supplier,"orderedQuantity",success,message FROM domae_urgent_logs WHERE "urgentOrderId"=%s', (uo,))
        return cur.fetchall()


def test_fill_commits_and_uses_db_remaining(env):
    sc, _, db, _ = env
    uo, mid, creds = setup(env, filled=4)
    sc._crawlers = {'인천': make_crawler()}
    r = run(env, uo, creds)
    assert sc._crawlers['인천'].orders == [('P1', 6)]
    assert read_urgent(db, uo) == (10, False, False, None, True)
    assert (r.filled, r.total_filled, r.total_qty, r.claimed, r.completed) == (6, 10, 10, True, True)


@pytest.mark.parametrize('reason', ['not_sent', 'stock_zero'])
@pytest.mark.parametrize('adjusted,fulfilled', [(3, 0), (3, 2), (3, 4), (None, 3)])
def test_failed_adjustment_receipt_halts_before_next_supplier(env, reason, adjusted, fulfilled):
    sc, pool, db, alerts = env
    uo, mid, creds = setup(env, suppliers=(('인천', 'P1'), ('백제', 'P1')))
    first = make_crawler(results=[OrderResult(reason_code=reason, adjusted_quantity=adjusted,
        fulfilled_quantity=fulfilled)])
    second = make_crawler()
    sc._crawlers = {'인천': first, '백제': second}
    result = run(env, uo, creds)
    assert first.orders == [('P1', 10)] and second.orders == []
    assert read_urgent(db, uo) == (max(3, fulfilled), False, True, None, False)
    assert result.halted and result.filled == max(3, fulfilled)
    assert not pool._used


def test_partial_fill_commits_before_next_supplier(env):
    sc, _, db, _ = env
    uo, mid, creds = setup(env, suppliers=(('인천', 'P1'), ('백제', 'P1')))
    def observe(c):
        assert read_urgent(db, uo)[:2] == (4, False)
    sc._crawlers = {'인천': make_crawler(4), '백제': make_crawler(9, before_search=observe)}
    r = run(env, uo, creds)
    assert sc._crawlers['백제'].orders == [('P1', 6)] and r.filled == 10


@pytest.mark.parametrize('result,filled', [(OrderResult(success=False, reason_code='send_unknown'), 0),
    (OrderResult(success=False, reason_code='send_unknown', fulfilled_quantity=3), 3)])
def test_halt_preserves_receipt_and_stops(env, result, filled):
    sc, _, db, _ = env
    uo, mid, creds = setup(env, suppliers=(('인천', 'P1'), ('백제', 'P1')))
    sc._crawlers = {'인천': make_crawler(9, [result]), '백제': make_crawler()}
    r = run(env, uo, creds)
    assert read_urgent(db, uo) == (filled, False, True, None, False)
    assert r.halted and sc._crawlers['백제'].orders == []
    with db.connection() as conn, conn.cursor() as cur:
        cur.execute('SELECT "checkRevision" FROM domae_urgent_orders WHERE id=%s', (uo,))
        assert cur.fetchone() == (1,)


@pytest.mark.parametrize('stock', [0, True, 1.5, None])
def test_no_valid_stock_does_not_claim(env, stock):
    sc, _, db, _ = env
    uo, mid, creds = setup(env)
    sc._crawlers = {'인천': make_crawler(stock)}
    r = run(env, uo, creds)
    assert read_urgent(db, uo) == (0, True, False, None, False)
    assert not r.claimed and not sc._crawlers['인천'].orders


@pytest.mark.parametrize('cols', [{'active':False}, {'active':False, 'checkRequired':True},
    {'active':False, 'sendingToken':'owner'}, {'checkRequired':True}, {'sendingToken':'owner'}])
def test_protected_state_does_not_login(env, cols):
    sc, _, db, _ = env
    uo, mid, creds = setup(env, **cols)
    sc._crawlers = {'인천': make_crawler()}
    assert not run(env, uo, creds).claimed and not sc._crawlers['인천'].logins


def test_race_changed_filled_rejects_claim(env):
    sc, _, db, _ = env
    uo, mid, creds = setup(env)
    sc._crawlers = {'인천': make_crawler(before_search=lambda c: update(db, uo, '"filledQuantity"=2'))}
    r = run(env, uo, creds)
    assert not sc._crawlers['인천'].orders and read_urgent(db, uo)[0] == 2


@pytest.mark.parametrize('safe,switch', [(False, None), (True, '0')])
def test_stock_only_login_and_dedup(env, monkeypatch, safe, switch):
    sc, _, db, _ = env
    if switch is not None:
        monkeypatch.setenv('DOMAE_URGENT_AUTO_ORDER', switch)
    uo, mid, creds = setup(env)
    sc._crawlers = {'인천': make_crawler(safe=safe)}
    first, second = run(env, uo, creds), run(env, uo, creds)
    assert first.stock_alerts and not second.stock_alerts
    assert not sc._crawlers['인천'].orders and sc._crawlers['인천'].logins
    assert read_urgent(db, uo) == (0, True, False, None, False)
    key = f'domae:urgent:stockalert:{uo}:인천'
    assert 21590 <= sc._redis.ttl(key) <= 21600


def test_login_failure_does_not_search_or_claim(env):
    sc, _, db, _ = env
    uo, mid, creds = setup(env)
    sc._crawlers = {'인천': make_crawler(login=False)}
    assert not run(env, uo, creds).claimed
    assert not sc._crawlers['인천'].orders and not sc._redis.keys('domae:urgent:stockalert:*')


def test_snapshot_bound_to_account(env):
    sc, _, db, _ = env
    uo, mid, creds = setup(env)
    sc._crawlers = {'인천': make_crawler(stock=0)}
    run(env, uo, creds)
    from domae_mcp.core.crawlers.cart_snapshot import CartSnapshot
    actual = sc._crawlers['인천'].instances[0].cart_snapshot
    expected = CartSnapshot(sc._redis, mid, '인천', account='local-test')
    assert actual.key == expected.key and actual.account_binding == expected.account_binding


def test_same_supplier_accumulates_and_position_orders(env):
    sc, _, db, _ = env
    uo, mid, creds = setup(env, total=5, suppliers=(('백제', 'P2'), ('백제', 'P1'), ('인천', 'P1')))
    sc._crawlers = {'백제': make_crawler(3, [OrderResult(success=True), OrderResult(success=True)]),
                    '인천': make_crawler()}
    r = run(env, uo, creds)
    assert sc._crawlers['백제'].orders == [('P2', 3), ('P1', 2)]
    assert not sc._crawlers['인천'].orders
    assert r.supplier_results['백제']['quantity'] == 5


@pytest.mark.parametrize('receipt', [OrderResult(success=True),
    OrderResult(success=False, reason_code='send_unknown', fulfilled_quantity=3)])
def test_late_receipt_logs_and_alerts_without_state_write(env, receipt):
    sc, _, db, alerts = env
    uo, mid, creds = setup(env, suppliers=(('인천','P1'),('백제','P1')))
    def recover(c):
        update(db, uo, '"checkRequired"=true,"sendingToken"=NULL,"sendingAt"=NULL')
    sc._crawlers = {'인천': make_crawler(4, [receipt], on_order=recover), '백제': make_crawler()}
    r = run(env, uo, creds)
    assert r.lost and not sc._crawlers['백제'].orders
    assert read_urgent(db, uo) == (0, False, True, None, False)
    qty = 4 if receipt.success else 3
    entries = logs(db, uo)
    assert any(s == '인천' and q == qty and ok for s,q,ok,msg in entries)
    assert any('수동 정산 필요' in str(a) for a in alerts)


def test_guard_stops_next_stage_after_owner_recovered(env):
    sc, _, db, alerts = env
    uo, mid, creds = setup(env)
    sends = []
    Base = make_crawler()
    class Staged(Base):
        def order(self, pid, qty, **kw):
            self.send_guard()
            sends.append(3)
            update(db, uo, '"checkRequired"=true,"sendingToken"=NULL,"sendingAt"=NULL')
            with pytest.raises(sch.ClaimLost):
                self.send_guard()
            return OrderResult(success=True, adjusted_quantity=3)
    sc._crawlers = {'인천': Staged}
    assert run(env, uo, creds).lost and sends == [3]
    assert logs(db, uo)[0][1] == 3


def test_system_exit_leaves_claim_then_recovery_blocks(env):
    sc, pool, db, alerts = env
    uo, mid, creds = setup(env)
    def crash(c):
        raise SystemExit('worker terminated')
    sc._crawlers = {'인천': make_crawler(on_order=crash)}
    with pytest.raises(SystemExit):
        run(env, uo, creds)
    assert read_urgent(db, uo)[3] is not None
    update(db, uo, '"sendingAt"=now()-interval \'31 minutes\'')
    conn = pool.getconn()
    try:
        with conn.cursor() as cur:
            sc._recover_stale_urgent(conn, cur, mid)
    finally:
        conn.rollback()
        pool.putconn(conn)
    assert read_urgent(db, uo) == (0, False, True, None, False)
    assert any('종료' in str(a) and '재시작' in str(a) for a in alerts)


def test_deadline_preclaim_orders_nothing(env, monkeypatch):
    sc, _, db, _ = env
    uo, mid, creds = setup(env)
    clock = [0]
    monkeypatch.setattr(sch.time, 'monotonic', lambda: clock[0])
    sc._crawlers = {'인천': make_crawler(before_search=lambda c: clock.__setitem__(0, 601))}
    assert not run(env, uo, creds).claimed and not sc._crawlers['인천'].orders
    assert read_urgent(db, uo) == (0, True, False, None, False)


def test_deadline_rechecks_after_guard_db_read(env, monkeypatch):
    sc, pool, db, _ = env
    uo, mid, creds = setup(env)
    clock = [0]
    monkeypatch.setattr(sch.time, 'monotonic', lambda: clock[0])
    original_get, original_put = pool.getconn, pool.putconn
    class SlowRead:
        def __init__(self, connection):
            self.connection = connection
        def cursor(self):
            real = self.connection.cursor()
            class Cursor:
                def __enter__(self):
                    return self
                def __exit__(self, *args):
                    real.close()
                def execute(self, *args):
                    real.execute(*args)
                def fetchone(self):
                    row = real.fetchone()  # execute/fetch query against actual scoped PostgreSQL
                    clock[0] = 601
                    return row
            return Cursor()
        def __getattr__(self, name):
            return getattr(self.connection, name)
        def rollback(self):
            self.connection.rollback()
    def blocking_get(*args, **kwargs):
        connection = original_get(*args, **kwargs)
        return SlowRead(connection) if read_urgent(db, uo)[3] else connection
    def put(connection, *args, **kw):
        original_put(getattr(connection, 'connection', connection), *args, **kw)
    monkeypatch.setattr(pool, 'getconn', blocking_get)
    monkeypatch.setattr(pool, 'putconn', put)
    sc._crawlers = {'인천': make_crawler()}
    r = run(env, uo, creds)
    assert not sc._crawlers['인천'].orders and r.deadline_expired and not r.halted
    assert read_urgent(db, uo) == (0, True, False, None, False)


def test_normal_receipts_defer_audit_and_notification_to_finish(env):
    sc, _, db, alerts = env
    uo, mid, creds = setup(env)
    sc._crawlers = {'인천': make_crawler()}
    assert run(env, uo, creds).completed
    assert read_urgent(db, uo) == (10, False, False, None, True)
    assert logs(db, uo) == [] and alerts == []


def test_cart_sync_path_still_rejected(env):
    sc, _, db, _ = env
    uo, mid, creds = setup(env)
    C = make_crawler()
    C.SUPPORTS_CART_SYNC = True
    sc._crawlers = {'인천': C}
    assert not run(env, uo, creds).claimed and not C.orders


def test_two_claimers_only_one_wins(env):
    sc, _, db, _ = env
    uo, mid, creds = setup(env)
    barrier = Barrier(2)
    def claim():
        with db.connection() as conn, conn.cursor() as cur:
            barrier.wait()
            try:
                return sch._urgent_claim(conn, cur, uo, 0)
            except sch.ClaimLost:
                return None
    with ThreadPoolExecutor(2) as threads:
        tokens = list(threads.map(lambda _: claim(), range(2)))
    assert sum(t is not None for t in tokens) == 1


def test_guard_returns_independent_connection_even_if_rollback_fails(env, monkeypatch):
    sc, pool, db, _ = env
    uo, mid, creds = setup(env)
    original_get, original_put = pool.getconn, pool.putconn
    class CleanupFailure:
        def __init__(self, connection):
            self.connection = connection
        def cursor(self):
            return self.connection.cursor()
        def rollback(self):
            self.connection.rollback()
            raise RuntimeError('local cleanup failure')
    def get(*args, **kw):
        connection = original_get(*args, **kw)
        return CleanupFailure(connection) if read_urgent(db, uo)[3] else connection
    def put(connection, *args, **kw):
        original_put(getattr(connection, 'connection', connection), *args, **kw)
    monkeypatch.setattr(pool, 'getconn', get)
    monkeypatch.setattr(pool, 'putconn', put)
    sc._crawlers = {'인천': make_crawler()}
    run(env, uo, creds)
    assert not pool._used, 'guard 독립 연결을 누수하지 않아야 한다'


def test_owned_update_wrong_token_cannot_write(env):
    sc, pool, db, _ = env
    uo, mid, creds = setup(env, active=False, sendingToken='actual-owner')
    with db.connection() as conn, conn.cursor() as cur:
        with pytest.raises(sch.ClaimLost):
            sch._owned_update(conn, cur, uo, 'wrong-owner', '"filledQuantity"=7')
    assert read_urgent(db, uo) == (0, False, False, 'actual-owner', False)


def test_skip_after_claim_reactivates_and_next_supplier_can_fill(env):
    sc, _, db, _ = env
    uo, mid, creds = setup(env, suppliers=(('인천','P1'),('백제','P1')))
    sc._crawlers = {'인천': make_crawler(9, [OrderResult(success=False, reason_code='not_sent')]),
                    '백제': make_crawler(4)}
    r = run(env, uo, creds)
    assert r.filled == 4 and not r.halted
    assert read_urgent(db, uo) == (4, True, False, None, False)


def test_missing_creds_and_crawler_skipped(env):
    sc, _, db, _ = env
    uo, mid, creds = setup(env, suppliers=(('인천','P1'),('백제','P1')))
    sc._crawlers = {'인천': make_crawler()}
    del creds['인천']
    r = run(env, uo, creds)
    assert not r.claimed and not sc._crawlers['인천'].logins
    assert read_urgent(db, uo) == (0, True, False, None, False)


def test_supplier_equal_position_breaks_tie_by_id(env):
    sc, _, db, _ = env
    uo, mid, creds = setup(env, total=1, suppliers=(('인천','P1'),('백제','P1')))
    with db.connection() as conn, conn.cursor() as cur:
        cur.execute('''UPDATE domae_urgent_suppliers SET position=0,id=CASE supplier
            WHEN '백제' THEN 'a_supplier' ELSE 'z_supplier' END WHERE "urgentOrderId"=%s''', (uo,))
    sc._crawlers = {'인천': make_crawler(), '백제': make_crawler()}
    run(env, uo, creds)
    assert sc._crawlers['백제'].orders == [('P1',1)] and not sc._crawlers['인천'].orders


def test_recovery_only_old_claims_for_requested_monitor(env):
    sc, pool, db, alerts = env
    uo, mid, creds = setup(env, active=False, sendingToken='expired',
        sendingAt=datetime.now(timezone.utc).replace(tzinfo=None)-timedelta(minutes=31), checkRevision=2)
    fresh = seed_urgent(db, mid, active=False, sendingToken='fresh', sendingAt=datetime.now(timezone.utc).replace(tzinfo=None))
    other_mid = seed_monitor(db, ('인천',))
    other = seed_urgent(db, other_mid, active=False, sendingToken='other',
        sendingAt=datetime.now(timezone.utc).replace(tzinfo=None)-timedelta(minutes=31))
    with db.connection() as conn, conn.cursor() as cur:
        assert sc._recover_stale_urgent(conn, cur, mid) == [uo]
        assert sc._recover_stale_urgent(conn, cur, mid) == []
        cur.execute('SELECT "checkRevision" FROM domae_urgent_orders WHERE id=%s', (uo,))
        assert cur.fetchone() == (3,)
    assert read_urgent(db, fresh)[3] == 'fresh' and read_urgent(db, other)[3] == 'other'


@pytest.mark.parametrize('phase', ['late', 'recovery'])
def test_protected_state_and_audit_survive_notification_failure(env, monkeypatch, phase):
    sc, pool, db, alerts = env
    uo, mid, creds = setup(env)
    def fail(*a, **kw):
        raise RuntimeError('local notifier failure')
    monkeypatch.setattr(Notifier, 'send_telegram', fail)
    if phase == 'late':
        sc._crawlers = {'인천': make_crawler(4, on_order=lambda c: update(db, uo,
            '"checkRequired"=true,"sendingToken"=NULL,"sendingAt"=NULL'))}
        assert run(env, uo, creds).lost
        assert logs(db, uo)[0][1] == 4
    else:
        update(db, uo, "active=false,\"sendingToken\"='old',\"sendingAt\"=now()-interval '31 minutes'")
        with db.connection() as conn, conn.cursor() as cur:
            assert sc._recover_stale_urgent(conn, cur, mid) == [uo]
    assert read_urgent(db, uo) == (0, False, True, None, False)


def test_late_receipt_audit_failure_still_alerts_and_keeps_protected_state(env):
    sc, pool, db, alerts = env
    uo, mid, creds = setup(env)
    with db.connection() as conn, conn.cursor() as cur:
        cur.execute("""CREATE FUNCTION refuse_urgent_log() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN RAISE EXCEPTION 'local audit failure'; END $$;
            CREATE TRIGGER refuse_log BEFORE INSERT ON domae_urgent_logs
            FOR EACH ROW EXECUTE FUNCTION refuse_urgent_log();""")
    sc._crawlers = {'인천': make_crawler(4, on_order=lambda c: update(db, uo,
        '"checkRequired"=true,"sendingToken"=NULL,"sendingAt"=NULL'))}
    r = run(env, uo, creds)
    assert r.lost and read_urgent(db, uo) == (0, False, True, None, False)
    assert any('수동 정산 필요' in str(alert) and '4개' in str(alert) for alert in alerts)


@pytest.mark.parametrize('receipt,confirmed', [
    (OrderResult(success=True), 4),
    (OrderResult(success=False, reason_code='send_unknown', fulfilled_quantity=3), 3),
])
def test_late_receipt_evidence_survives_audit_cleanup_and_notifier_failure(
        env, monkeypatch, caplog, receipt, confirmed):
    sc, pool, db, alerts = env
    uo, mid, creds = setup(env)
    with db.connection() as connection, connection.cursor() as cur:
        cur.execute("""CREATE FUNCTION refuse_urgent_log() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN RAISE EXCEPTION 'local audit failure'; END $$;
            CREATE TRIGGER refuse_log BEFORE INSERT ON domae_urgent_logs
            FOR EACH ROW EXECUTE FUNCTION refuse_urgent_log();""")
    sc._crawlers = {'인천': make_crawler(4, [receipt], on_order=lambda c: update(db, uo,
        '"checkRequired"=true,"sendingToken"=NULL,"sendingAt"=NULL'))}
    notification_attempts, rollback_attempts = [], []
    def notifier_failure(chat_id, message, **kw):
        notification_attempts.append(message)
        raise RuntimeError('local notifier failure')
    monkeypatch.setattr(Notifier, 'send_telegram', notifier_failure)
    class BrokenCleanup:
        # Claims, token-conditioned updates and the rejected INSERT use real PostgreSQL.
        # Only connection cleanup failure is injected at the DB connection boundary.
        def __init__(self, connection):
            self.connection = connection
        def __getattr__(self, name):
            return getattr(self.connection, name)
        def rollback(self):
            rollback_attempts.append(True)
            raise RuntimeError('local connection cleanup failure')
    original_get, original_put = sc._get_conn, pool.putconn
    discarded = []
    monkeypatch.setattr(sc, '_get_conn', lambda: BrokenCleanup(original_get()))
    def return_audit(connection, **kwargs):
        if isinstance(connection, BrokenCleanup):
            discarded.append(kwargs.get('close'))
            connection = connection.connection
        return original_put(connection, **kwargs)
    monkeypatch.setattr(pool, 'putconn', return_audit)
    r = run(env, uo, creds)
    assert discarded == [True] and not pool._used
    assert r.lost and rollback_attempts == [True]
    assert read_urgent(db, uo) == (0, False, True, None, False) and logs(db, uo) == []
    assert len(notification_attempts) == 1
    assert '인천' in notification_attempts[0] and f'{confirmed}개' in notification_attempts[0]
    assert any('supplier=인천' in record.getMessage() and f'quantity={confirmed}' in record.getMessage()
               and '수동 정산 필요' in record.getMessage() for record in caplog.records)


def immediate(env, uo, mid):
    sc = env[0]
    key = 'urgent-response:' + uo
    sc.urgent_order_immediate({'monitor_id': mid, 'action': 'urgent_order_immediate',
        'response_key': key, 'urgent_order_id': uo, 'product_name': '오래된 다른 품목',
        'remaining_quantity': 10, 'suppliers': [{'supplier': '없는 도매', 'product_id': 'STALE'}]})
    assert sc._redis.llen(key) == 1
    return json.loads(sc._redis.lpop(key))


def periodic(env, mid, creds):
    sc, pool, _, _ = env
    connection = pool.getconn()
    try:
        sc._process_urgent_orders(connection, mid, creds)
    finally:
        connection.rollback()
        pool.putconn(connection)


def test_c4_immediate_ignores_stale_job_and_returns_current_state(env):
    sc, pool, db, alerts = env
    uo, mid, creds = setup(env, filled=4)
    sc._crawlers = {'인천': make_crawler()}
    payload = immediate(env, uo, mid)
    assert sc._crawlers['인천'].orders == [('P1', 6)]
    assert (payload['state'], payload['total_filled'], payload['filled_quantity']) == ('completed', 10, 6)
    assert logs(db, uo)[0][:3] == ('인천', 6, True)
    assert alerts and not pool._used


@pytest.mark.parametrize('cols,state', [({'checkRequired':True,'active':False}, 'check_required'),
    ({'sendingToken':'owner','active':False}, 'sending'), ({'active':False}, 'cancelled')])
def test_c4_protected_immediate_responds_without_order(env, cols, state):
    sc, _, db, _ = env
    uo, mid, creds = setup(env, **cols)
    sc._crawlers = {'인천': make_crawler()}
    assert immediate(env, uo, mid)['state'] == state
    assert not sc._crawlers['인천'].orders and logs(db, uo) == []


@pytest.mark.parametrize('receipt,confirmed', [(OrderResult(success=True), 10),
    (OrderResult(success=True, adjusted_quantity=4), 4),
    (OrderResult(success=False, reason_code='send_unknown', fulfilled_quantity=3), 3)])
def test_c4_postclaim_database_failure_still_returns_sending(env, caplog, receipt, confirmed):
    sc, pool, db, _ = env
    uo, mid, creds = setup(env)
    with db.connection() as connection, connection.cursor() as cur:
        cur.execute("""CREATE FUNCTION refuse_receipt_update() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN IF NEW."filledQuantity" <> OLD."filledQuantity" THEN
                RAISE EXCEPTION 'local receipt update failure'; END IF; RETURN NEW; END $$;
            CREATE TRIGGER refuse_receipt BEFORE UPDATE ON domae_urgent_orders
            FOR EACH ROW EXECUTE FUNCTION refuse_receipt_update();""")
    sc._crawlers = {'인천': make_crawler(results=[receipt])}
    payload = immediate(env, uo, mid)
    assert payload['state'] == 'sending' and payload['total_filled'] == 0
    assert read_urgent(db, uo)[3] and not pool._used
    assert any('supplier=인천' in record.getMessage() and f'quantity={confirmed}' in record.getMessage()
               and 'DB' in record.getMessage() and '수동' in record.getMessage() for record in caplog.records)


def test_c4_response_requeries_changes_made_during_notification(env, monkeypatch):
    sc, _, db, _ = env
    uo, mid, creds = setup(env)
    sc._crawlers = {'인천': make_crawler()}
    monkeypatch.setattr(Notifier, 'send_urgent_order_result', lambda **kw:
        update(db, uo, '"checkRequired"=true,"checkReason"=%s', ('외부 확인 필요',)))
    payload = immediate(env, uo, mid)
    assert payload['state'] == 'check_required' and payload['check_reason'] == '외부 확인 필요'


@pytest.mark.parametrize('inactive', [False, True])
def test_c4_wrong_monitor_or_inactive_monitor_never_orders(env, inactive):
    sc, _, db, _ = env
    uo, mid, creds = setup(env)
    other = seed_monitor(db, ('인천',))
    if inactive:
        with db.connection() as connection, connection.cursor() as cur:
            cur.execute('UPDATE domae_cloud_monitors SET "isActive"=false WHERE id=%s', (mid,))
    sc._crawlers = {'인천': make_crawler()}
    payload = immediate(env, uo, mid if inactive else other)
    assert not payload['success'] and not sc._crawlers['인천'].orders
    if not inactive:
        assert payload['state'] == 'not_found' and payload['message'] == '해당 긴급주문 없음'
    assert read_urgent(db, uo) == (0, True, False, None, False)


def test_c4_periodic_logs_cumulative_once_without_double_increment(env):
    sc, _, db, alerts = env
    uo, mid, creds = setup(env, total=5, suppliers=(('인천','P1'),('인천','P2')))
    sc._crawlers = {'인천': make_crawler(3, [OrderResult(success=True), OrderResult(success=True)])}
    periodic(env, mid, creds)
    assert read_urgent(db, uo) == (5, False, False, None, True)
    assert [entry[:3] for entry in logs(db, uo)] == [('인천', 5, True)]
    assert sum(kw['quantity'] for args, kw in alerts if 'quantity' in kw) == 5


def test_c4_retains_latest_twenty_logs(env):
    sc, _, db, _ = env
    uo, mid, creds = setup(env)
    with db.connection() as connection, connection.cursor() as cur:
        for i in range(22):
            cur.execute('''INSERT INTO domae_urgent_logs
                (id,"urgentOrderId",supplier,"orderedQuantity",success,"orderedAt")
                VALUES (%s,%s,'인천',0,false,now()-interval '1 day'+%s*interval '1 minute')''',
                (f'old_{i}', uo, i))
    sc._crawlers = {'인천': make_crawler()}
    immediate(env, uo, mid)
    assert len(logs(db, uo)) == 20 and any(q == 10 for _, q, _, _ in logs(db, uo))


@pytest.mark.parametrize('phase', ['audit', 'notification'])
def test_c4_finish_failures_keep_receipt_state_and_response(env, monkeypatch, phase):
    sc, pool, db, alerts = env
    uo, mid, creds = setup(env)
    if phase == 'audit':
        with db.connection() as connection, connection.cursor() as cur:
            cur.execute("""CREATE FUNCTION refuse_log_c4() RETURNS trigger LANGUAGE plpgsql AS $$
                BEGIN RAISE EXCEPTION 'local audit failure'; END $$;
                CREATE TRIGGER refuse_log BEFORE INSERT ON domae_urgent_logs
                FOR EACH ROW EXECUTE FUNCTION refuse_log_c4();""")
    else:
        def fail(**kw):
            raise RuntimeError('local notifier failure')
        monkeypatch.setattr(Notifier, 'send_urgent_order_result', fail)
    sc._crawlers = {'인천': make_crawler()}
    payload = immediate(env, uo, mid)
    assert payload['state'] == 'completed' and payload['total_filled'] == 10
    assert read_urgent(db, uo) == (10, False, False, None, True) and not pool._used
    if phase == 'audit':
        assert alerts, '감사 실패가 체결 알림을 막으면 안 된다'


def test_c4_partial_unknown_notifies_and_logs_only_committed_receipt(env):
    sc, _, db, alerts = env
    uo, mid, creds = setup(env)
    sc._crawlers = {'인천': make_crawler(9, [OrderResult(success=False,
        reason_code='send_unknown', fulfilled_quantity=3)])}
    payload = immediate(env, uo, mid)
    assert payload['state'] == 'check_required' and payload['total_filled'] == 3
    assert [entry[:3] for entry in logs(db, uo)] == [('인천', 3, True)]
    assert any('확인 필요' in str(a) for a in alerts)


def test_c4_late_receipt_not_duplicated_by_finish(env):
    sc, _, db, alerts = env
    uo, mid, creds = setup(env)
    sc._crawlers = {'인천': make_crawler(4, on_order=lambda c: update(db, uo,
        '"checkRequired"=true,"sendingToken"=NULL,"sendingAt"=NULL'))}
    payload = immediate(env, uo, mid)
    assert payload['state'] == 'check_required' and payload['total_filled'] == 0
    assert len(logs(db, uo)) == 1 and logs(db, uo)[0][1] == 4
    assert sum('수동 정산 필요' in str(a) for a in alerts) == 1


@pytest.mark.parametrize('receipt,confirmed', [(OrderResult(success=True), 4),
    (OrderResult(success=False, reason_code='send_unknown', fulfilled_quantity=3), 3)])
def test_c4_broken_execution_connection_discards_and_requeries_state(
        env, monkeypatch, caplog, receipt, confirmed):
    sc, pool, db, alerts = env
    uo, mid, creds = setup(env)
    with db.connection() as connection, connection.cursor() as cur:
        cur.execute("""CREATE FUNCTION refuse_all_audit() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN RAISE EXCEPTION 'local audit failure'; END $$;
            CREATE TRIGGER refuse_audit BEFORE INSERT ON domae_urgent_logs
            FOR EACH ROW EXECUTE FUNCTION refuse_all_audit();""")
    sc._crawlers = {'인천': make_crawler(4, [receipt], on_order=lambda c: update(db, uo,
        '"checkRequired"=true,"sendingToken"=NULL,"sendingAt"=NULL'))}
    original_get, original_put = sc._get_conn, pool.putconn
    discarded, first = [], [True]
    class BrokenCleanup:
        def __init__(self, connection):
            self.connection = connection
        def __getattr__(self, name):
            return getattr(self.connection, name)
        def rollback(self):
            raise RuntimeError('local caller cleanup failure')
    def get():
        connection = original_get()
        if first[0]:
            first[0] = False
            return BrokenCleanup(connection)
        return connection
    def put(connection, *args, **kwargs):
        if isinstance(connection, BrokenCleanup):
            discarded.append(kwargs.get('close'))
            connection = connection.connection
        original_put(connection, *args, **kwargs)
    def notifier_failure(*args, **kwargs):
        raise RuntimeError('local notifier failure')
    monkeypatch.setattr(sc, '_get_conn', get)
    monkeypatch.setattr(pool, 'putconn', put)
    monkeypatch.setattr(Notifier, 'send_telegram', notifier_failure)
    payload = immediate(env, uo, mid)
    assert payload['state'] == 'check_required' and payload['total_filled'] == 0
    assert discarded == [True] and not pool._used
    assert any(f'quantity={confirmed}' in record.getMessage() for record in caplog.records)


def test_c4_periodic_failure_does_not_block_next_run(env):
    sc, pool, db, alerts = env
    uo, mid, creds = setup(env)
    other = seed_urgent(db, mid, total=2)
    with db.connection() as connection, connection.cursor() as cur:
        cur.execute("""CREATE FUNCTION refuse_first_run() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN IF NEW.id=%s AND NEW."filledQuantity" <> OLD."filledQuantity" THEN
                RAISE EXCEPTION 'local first-run failure'; END IF; RETURN NEW; END $$;
            CREATE TRIGGER fail_first BEFORE UPDATE ON domae_urgent_orders
            FOR EACH ROW EXECUTE FUNCTION refuse_first_run();""", (uo,))
    sc._crawlers = {'인천': make_crawler(results=[OrderResult(success=True), OrderResult(success=True)])}
    periodic(env, mid, creds)
    assert read_urgent(db, uo)[3] and read_urgent(db, other) == (2, False, False, None, True)
    assert not pool._used


def test_c4_periodic_recovers_stale_and_skips_protected_rows(env):
    sc, pool, db, alerts = env
    uo, mid, creds = setup(env, active=False, sendingToken='old',
        sendingAt=datetime.now(timezone.utc).replace(tzinfo=None)-timedelta(minutes=31))
    protected = seed_urgent(db, mid, checkRequired=True)
    sc._crawlers = {'인천': make_crawler()}
    periodic(env, mid, creds)
    assert read_urgent(db, uo) == (0, False, True, None, False)
    assert not sc._crawlers['인천'].orders and logs(db, protected) == []
    assert any('재시작' in str(a) for a in alerts)


def test_c4_stock_only_warning_is_deduplicated_and_html_safe(env, monkeypatch):
    sc, pool, db, alerts = env
    uo, mid, creds = setup(env)
    with db.connection() as connection, connection.cursor() as cur:
        cur.execute('UPDATE domae_urgent_orders SET "productName"=%s WHERE id=%s', ('<약&품>', uo))
    sc._crawlers = {'인천': make_crawler(safe=False)}
    first, second = immediate(env, uo, mid), immediate(env, uo, mid)
    assert first['stock_alerts'] and not second['stock_alerts']
    assert first['state'] == 'waiting' and not sc._crawlers['인천'].orders
    warnings = [str(a) for a in alerts if '직접 주문' in str(a)]
    assert len(warnings) == 1 and '&lt;약&amp;품&gt;' in warnings[0]


def test_c4_missing_urgent_row_still_has_one_response(env):
    sc, pool, db, alerts = env
    mid = seed_monitor(db, ('인천',))
    payload = immediate(env, 'missing-urgent', mid)
    assert not payload['success'] and payload['state'] == 'not_found' and not pool._used


def test_c4_failed_final_state_query_is_unknown_without_losing_receipt(env, monkeypatch):
    sc, pool, db, alerts = env
    uo, mid, creds = setup(env)
    def disable_state_query(**kw):
        with db.connection() as connection, connection.cursor() as cur:
            cur.execute('ALTER TABLE domae_urgent_orders RENAME COLUMN "checkReason" TO "unavailableReason"')
    monkeypatch.setattr(Notifier, 'send_urgent_order_result', disable_state_query)
    sc._crawlers = {'인천': make_crawler()}
    payload = immediate(env, uo, mid)
    assert payload['state'] == 'unknown' and payload['filled_quantity'] == 10
    assert read_urgent(db, uo) == (10, False, False, None, True) and not pool._used


def test_c4_serial_wrappers_fit_runtime_three_connection_budget(env, monkeypatch):
    sc, pool, db, alerts = env
    uo, mid, creds = setup(env, total=4)
    next_uo = seed_urgent(db, mid, total=3)
    sc._crawlers = {'인천': make_crawler(results=[OrderResult(success=True), OrderResult(success=True)])}
    original_get = pool.getconn
    occupancies = []
    def get(*args, **kwargs):
        connection = original_get(*args, **kwargs)
        occupancies.append(len(pool._used))
        return connection
    def notifier_failure(**kwargs):
        raise RuntimeError('local serial notifier failure')
    monkeypatch.setattr(pool, 'getconn', get)
    monkeypatch.setattr(Notifier, 'send_urgent_order_result', notifier_failure)
    assert pool.maxconn == 3
    payload = immediate(env, uo, mid)
    assert (payload['state'], payload['total_filled'], payload['filled_quantity']) == ('completed', 4, 4)
    assert not pool._used, '즉시 실행과 새 상태 조회의 연결이 모두 반환되어야 한다'
    periodic(env, mid, creds)
    assert sc._crawlers['인천'].orders == [('P1', 4), ('P1', 3)]
    assert [entry[:3] for entry in logs(db, uo)] == [('인천', 4, True)]
    assert [entry[:3] for entry in logs(db, next_uo)] == [('인천', 3, True)]
    assert read_urgent(db, next_uo) == (3, False, False, None, True)
    assert max(occupancies) == 3 and all(used <= 3 for used in occupancies)
    assert not pool._used, '주기 실행의 호출부·실행·guard·감사 연결이 모두 반환되어야 한다'
