"""B R4/R7 actual local PG boundaries; suppliers and notifications are substituted."""
import sys
sys.path.insert(0, "src")
import pytest
from domae_mcp.cloud import scheduler as sch
from domae_mcp.core.crawlers.base import OrderResult
from domae_mcp.cloud.notifier import Notifier
from tests.test_urgent_db import env, setup, make_crawler, run, update, logs, immediate, periodic
from tests.urgent_db_fixture import urgent_database, read_urgent, seed_urgent


@pytest.mark.parametrize('wrapper', ['periodic', 'immediate'])
@pytest.mark.parametrize('failure', ['redis', 'general'])
def test_partial_exception_callers_keep_same_run_and_finish_once(env, monkeypatch, wrapper, failure):
    sc, pool, db, alerts = env
    uo, mid, creds = setup(env, suppliers=(('인천','P1'), ('백제','P1')))
    first, second = make_crawler(4), make_crawler(safe=False)
    sc._crawlers = {'인천': first, '백제': second}
    if failure == 'redis':
        monkeypatch.setattr(sc._redis, 'set', lambda *a, **k: (_ for _ in ()).throw(RuntimeError('local Redis failure')))
    else:
        monkeypatch.setattr(sch, 'find_listing', lambda *a: (_ for _ in ()).throw(RuntimeError('local search failure')))
    fill, finish = sc._urgent_fill, sc._finish_urgent_run
    held, finished = [], []
    def capture_fill(*args, **kwargs):
        held.append(kwargs.get('run'))
        return fill(*args, **kwargs)
    def capture_finish(*args):
        finished.append(args[-1])
        assert args[-1].finish_attempted
        return finish(*args)
    monkeypatch.setattr(sc, '_urgent_fill', capture_fill)
    monkeypatch.setattr(sc, '_finish_urgent_run', capture_finish)
    payload = immediate(env, uo, mid) if wrapper == 'immediate' else periodic(env, mid, creds)
    assert held[0] is not None and finished == held
    assert read_urgent(db, uo)[0] == 4 and read_urgent(db, uo)[3]
    assert logs(db, uo)[0][:3] == ('인천', 4, True)
    assert sum(kw.get('quantity', 0) for _, kw in alerts) == 4
    if payload: assert payload['filled_quantity'] == 4 and payload['state'] == 'sending'
    before = len(logs(db, uo)), len(alerts)
    finish(None, None, mid, uo, held[0])
    assert (len(logs(db, uo)), len(alerts)) == before
    assert not pool._used


def test_deadline_after_committed_partial_releases_only_owned_token(env, monkeypatch):
    sc, pool, db, alerts = env
    uo, mid, creds = setup(env, suppliers=(('인천','P1'), ('백제','P1')))
    clock = [0]
    monkeypatch.setattr(sch.time, 'monotonic', lambda: clock[0])
    first = make_crawler(4)
    second = make_crawler(before_search=lambda _: clock.__setitem__(0, 601))
    sc._crawlers = {'인천': first, '백제': second}
    result = run(env, uo, creds)
    assert read_urgent(db, uo) == (4, True, False, None, False)
    assert result.filled == 4 and not result.lost and result.deadline_expired
    assert second.orders == []
    with db.connection() as conn, conn.cursor() as cur:
        assert sc._recover_stale_urgent(conn, cur, mid) == []


@pytest.mark.parametrize('quantity', [0, 3])
def test_late_unknown_has_real_supplier_audit_and_one_notification(env, quantity):
    sc, pool, db, alerts = env
    uo, mid, creds = setup(env, suppliers=(('인천','P1'), ('백제','P1')))
    sc._crawlers = {'인천': make_crawler(9, [OrderResult(success=False, reason_code='send_unknown', fulfilled_quantity=quantity)],
        on_order=lambda _: update(db, uo, '"sendingToken"=NULL,"checkRequired"=true')), '백제': make_crawler()}
    payload = immediate(env, uo, mid)
    records = logs(db, uo)
    assert len(records) == 1 and records[0][:3] == ('인천', quantity, quantity > 0)
    message = records[0][3]
    assert message.startswith('⚠ 실행 소유권 상실 뒤 전송 결과 불명:')
    assert uo in message and '인천' in message and '수동' in message
    assert '확정된 접수량' in message and '추가 접수 불명' in message
    assert len(alerts) == 1 and message in str(alerts)
    assert payload['filled_quantity'] == 0 and read_urgent(db, uo)[0] == 0
    assert sc._crawlers['백제'].orders == [] and not pool._used


@pytest.mark.parametrize('supplier_reason', ['success', 'unknown'])
def test_deadline_inside_stage_distinguishes_confirmed_from_unknown(env, monkeypatch, supplier_reason):
    sc, pool, db, alerts = env
    uo, mid, creds = setup(env)
    clock = [0]
    monkeypatch.setattr(sch.time, 'monotonic', lambda: clock[0])
    Base = make_crawler()
    class Staged(Base):
        def order(self, pid, qty, **kwargs):
            self.send_guard()
            clock[0] = 601
            try: self.send_guard()
            except Exception: pass  # Actual crawlers return confirmed partial or unknown after guard stop.
            return OrderResult(success=True, adjusted_quantity=3) if supplier_reason == 'success' else OrderResult(
                success=False, reason_code='send_unknown', fulfilled_quantity=3)
    sc._crawlers = {'인천': Staged}
    result = run(env, uo, creds)
    assert result.filled == 3 and read_urgent(db, uo)[0] == 3
    if supplier_reason == 'success':
        assert read_urgent(db, uo) == (3, True, False, None, False) and not result.lost
    else:
        assert read_urgent(db, uo) == (3, False, True, None, False) and result.halted


def test_propagated_deadline_after_prior_guard_pass_does_not_prove_unsent(env, monkeypatch):
    sc, pool, db, alerts = env
    uo, mid, creds = setup(env, suppliers=(('인천', 'P1'), ('백제', 'P1')))
    clock = [0]
    monkeypatch.setattr(sch.time, 'monotonic', lambda: clock[0])
    Base = make_crawler()
    class Staged(Base):
        def order(self, pid, qty, **kwargs):
            self.send_guard()
            self.orders.append((pid, qty))  # first stage may have reached the supplier
            clock[0] = 601
            self.send_guard()  # no receipt returned for the earlier stage
    sc._crawlers = {'인천': Staged, '백제': make_crawler()}
    result = run(env, uo, creds)
    assert result.halted and result.filled == 0
    assert read_urgent(db, uo) == (0, False, True, None, False)
    assert sc._crawlers['인천'].orders == [('P1', 10)]
    assert sc._crawlers['백제'].orders == []
    assert not pool._used


def test_retention_excludes_exact_audits_from_delete_and_ordinary_rank(env):
    sc, pool, db, alerts = env
    uo, mid, creds = setup(env)
    other = seed_urgent(db, mid)
    protected = ['⚠ 확인 처리 이후 도착한 체결: x', '⚠ 실행 소유권 상실 뒤 전송 결과 불명: x']
    with db.connection() as conn, conn.cursor() as cur:
        for target in (uo, other):
            for i in range(25):
                message = None if i % 2 else '⚠ 확인 처리 이후 도착한 체결 x'  # no colon => ordinary
                cur.execute('INSERT INTO domae_urgent_logs (id,"urgentOrderId",supplier,"orderedQuantity",success,message,"orderedAt") VALUES (%s,%s,%s,0,false,%s,%s)',
                    (target+f'_o{i:02}', target, '인천', message, '2026-01-01'))
            for i in range(4):
                cur.execute('INSERT INTO domae_urgent_logs (id,"urgentOrderId",supplier,"orderedQuantity",success,message,"orderedAt") VALUES (%s,%s,%s,0,false,%s,%s)',
                    (target+f'_a{i}', target, '확인처리' if i < 2 else '백제', None if i < 2 else protected[i-2], '2025-01-01'))
    result = sch.UrgentRun(supplier_results={'인천': {'quantity': 0}})
    sc._finish_urgent_run(None, None, mid, uo, result)
    records = logs(db, uo)
    assert len(records) == 24
    assert sum(s == '확인처리' or (msg or '').startswith(tuple(protected)) for s, q, ok, msg in records) == 4
    assert len(logs(db, other)) == 29
    with db.connection() as conn, conn.cursor() as cur:
        cur.execute('SELECT id FROM domae_urgent_logs WHERE "urgentOrderId"=%s AND id LIKE %s ORDER BY id', (uo, uo+'_o%'))
        assert [row[0] for row in cur.fetchall()] == [uo+f'_o{i:02}' for i in range(6,25)]


@pytest.mark.parametrize('wrapper', ['periodic', 'immediate'])
def test_pool_return_failure_prevents_finish_status_and_next_acquisition(env, monkeypatch, wrapper):
    sc, pool, db, alerts = env
    uo, mid, creds = setup(env, total=4)
    other = seed_urgent(db, mid, total=2)
    sc._crawlers = {'인천': make_crawler(results=[OrderResult(success=True), OrderResult(success=True)])}
    get, put = pool.getconn, pool.putconn
    gets = []; blocked = [False]; forbidden_gets = []
    def tracked_get(*a, **kw):
        if blocked[0]: forbidden_gets.append(True)
        assert not blocked[0], 'uncertain pool return must stop all later acquisitions'
        conn = get(*a, **kw); gets.append(conn); return conn
    def failed_return(conn, *a, **kw):
        # Guard returns first. Fail the executing connection return after the target is completed.
        if conn is gets[0 if wrapper == 'immediate' else 1] and read_urgent(db, uo)[4]:
            blocked[0] = True
            raise RuntimeError('local pool cleanup failure')
        return put(conn, *a, **kw)
    monkeypatch.setattr(pool, 'getconn', tracked_get)
    monkeypatch.setattr(pool, 'putconn', failed_return)
    payload = immediate(env, uo, mid) if wrapper == 'immediate' else periodic(env, mid, creds)
    assert blocked[0] and forbidden_gets == [] and sc._crawlers['인천'].orders == [('P1', 4)]
    assert read_urgent(db, other)[0] == 0 and read_urgent(db, uo)[0] == 4
    if payload: assert payload['state'] == 'unknown' and payload['filled_quantity'] == 4


def inject_connection_fault(env, monkeypatch, fault):
    """Wrap real scoped PG statements and commits; pool accounting stays real."""
    import psycopg2
    sc, pool, db, alerts = env
    get, put = pool.getconn, pool.putconn
    trace = {'gets': 0, 'after_unsafe': 0, 'unsafe': False, 'audit_inserts': 0, 'receipt_commits': 0,
             'discarded': [], 'direct_closes': 0, 'audit_connections': [], 'run_connections': []}
    class Connection:
        def __init__(self, connection): self.connection, self.phase = connection, 'unknown'
        def __getattr__(self, name): return getattr(self.connection, name)
        def cursor(self):
            real = self.connection.cursor(); owner = self
            class Cursor:
                def __getattr__(self, name): return getattr(real, name)
                def __enter__(self): return self
                def __exit__(self, *args): real.close()
                def execute(self, sql, params=None):
                    if 'FROM domae_urgent_orders WHERE id=' in sql or 'SELECT m.credentials' in sql:
                        owner.phase = 'run'; trace['run_connections'].append(owner.connection)
                    elif 'SELECT 1 FROM domae_urgent_orders' in sql:
                        owner.phase = 'guard'
                    elif 'INSERT INTO domae_urgent_logs' in sql:
                        owner.phase = 'audit'; trace['audit_inserts'] += 1
                        trace['audit_connections'].append(owner.connection)
                        if fault == 'audit_insert_rollback': return real.execute('SELECT 1/0')
                    elif '"filledQuantity"=LEAST' in sql:
                        owner.phase = 'receipt'
                    return real.execute(sql, params)
            return Cursor()
        def commit(self):
            self.connection.commit()
            if self.phase == 'receipt':
                trace['receipt_commits'] += 1
                if fault == 'receipt_commit_ack': raise RuntimeError('local receipt commit ACK lost')
            if self.phase == 'audit' and fault in ('audit_commit_ack', 'audit_commit_rollback'):
                raise psycopg2.OperationalError('local audit commit ACK lost')
        def rollback(self):
            if self.phase == 'guard' and fault == 'guard_rollback': raise psycopg2.InterfaceError('local guard rollback failure')
            if self.phase == 'audit' and fault in ('audit_insert_rollback', 'audit_commit_rollback'):
                raise psycopg2.InterfaceError('local audit rollback failure')
            return self.connection.rollback()
        def close(self): trace['direct_closes'] += 1; self.connection.close()
    def acquire(*args, **kwargs):
        trace['gets'] += 1
        if trace['unsafe']: trace['after_unsafe'] += 1
        return Connection(get(*args, **kwargs))
    def release(conn, *args, **kwargs):
        if isinstance(conn, Connection):
            if (fault == 'guard_return' and conn.phase == 'guard') or (fault == 'audit_return' and conn.phase == 'audit'):
                trace['unsafe'] = True
                raise RuntimeError('local slot return unknown')
            if kwargs.get('close'): trace['discarded'].append(conn.phase)
            conn = conn.connection
        return put(conn, *args, **kwargs)
    monkeypatch.setattr(pool, 'getconn', acquire)
    monkeypatch.setattr(pool, 'putconn', release)
    return trace


@pytest.mark.parametrize('wrapper', ['periodic', 'immediate'])
def test_receipt_actual_commit_response_loss_keeps_evidence_and_protects_token(env, monkeypatch, wrapper):
    sc, pool, db, alerts = env
    uo, mid, creds = setup(env, suppliers=(('인천','P1'), ('백제','P1')))
    sc._crawlers = {'인천': make_crawler(4), '백제': make_crawler()}
    trace = inject_connection_fault(env, monkeypatch, 'receipt_commit_ack')
    runs = []; fill = sc._urgent_fill
    def capture(*args, **kwargs):
        runs.append(kwargs['run']); return fill(*args, **kwargs)
    monkeypatch.setattr(sc, '_urgent_fill', capture)
    payload = immediate(env, uo, mid) if wrapper == 'immediate' else periodic(env, mid, creds)
    assert read_urgent(db, uo)[0] == 4 and read_urgent(db, uo)[3]  # actual commit, unknown ACK => no release
    assert trace['receipt_commits'] == 1 and sc._crawlers['백제'].orders == []
    assert runs[0].filled == 0 and runs[0].successes == []
    assert runs[0].unsettled[0]['quantity'] == 4
    assert any('DB 반영 불명' in str(alert) and '4개' in str(alert) for alert in alerts)
    if payload: assert payload['filled_quantity'] == 0 and payload['total_filled'] == 4


@pytest.mark.parametrize('fault', ['audit_commit_ack', 'audit_insert_rollback', 'audit_commit_rollback'])
@pytest.mark.parametrize('late', [False, True])
def test_audit_notification_failures_never_retry_live_attempts(env, monkeypatch, fault, late, caplog):
    sc, pool, db, alerts = env
    uo, mid, creds = setup(env, total=4)
    recover = (lambda _: update(db, uo, '"sendingToken"=NULL,"checkRequired"=true')) if late else None
    sc._crawlers = {'인천': make_crawler(4, on_order=recover)}
    trace = inject_connection_fault(env, monkeypatch, fault)
    attempts, runs = [], []
    def notification_failure(*args, **kwargs):
        attempts.append((args, kwargs)); raise RuntimeError('local Telegram ACK unknown')
    monkeypatch.setattr(Notifier, 'send_telegram', notification_failure)
    monkeypatch.setattr(Notifier, 'send_urgent_order_result', notification_failure)
    fill = sc._urgent_fill
    def capture(*args, **kwargs):
        runs.append(kwargs['run']); return fill(*args, **kwargs)
    monkeypatch.setattr(sc, '_urgent_fill', capture)
    immediate(env, uo, mid)
    before = trace['audit_inserts'], len(attempts), trace['gets']
    sc._finish_urgent_run(None, None, mid, uo, runs[0])
    assert (trace['audit_inserts'], len(attempts), trace['gets']) == before
    assert trace['audit_inserts'] == 1 and len(attempts) == 1
    assert len(logs(db, uo)) == (0 if fault == 'audit_insert_rollback' else 1)
    assert trace['discarded'] and not pool._used
    assert all(conn not in trace['run_connections'] for conn in trace['audit_connections'])
    assert read_urgent(db, uo)[0] == (0 if late else 4)
    assert any('supplier=인천' in record.message and 'quantity=4' in record.message for record in caplog.records)


@pytest.mark.parametrize('fault', ['guard_return', 'audit_return'])
@pytest.mark.parametrize('wrapper', ['periodic', 'immediate'])
def test_uncertain_guard_or_audit_return_stops_all_later_acquisition(env, monkeypatch, fault, wrapper):
    sc, pool, db, alerts = env
    uo, mid, creds = setup(env, total=4)
    other = seed_urgent(db, mid, total=2)
    sc._crawlers = {'인천': make_crawler(results=[OrderResult(success=True), OrderResult(success=True)])}
    trace = inject_connection_fault(env, monkeypatch, fault)
    payload = immediate(env, uo, mid) if wrapper == 'immediate' else periodic(env, mid, creds)
    assert trace['unsafe'] and trace['after_unsafe'] == 0 and trace['direct_closes'] == 1
    assert read_urgent(db, other)[0] == 0
    if fault == 'guard_return': assert sc._crawlers['인천'].orders == []
    if payload: assert payload['state'] == 'unknown'


def test_guard_rollback_failure_blocks_wire_and_discards_connection(env, monkeypatch):
    sc, pool, db, alerts = env
    uo, mid, creds = setup(env)
    sc._crawlers = {'인천': make_crawler()}
    trace = inject_connection_fault(env, monkeypatch, 'guard_rollback')
    immediate(env, uo, mid)
    assert sc._crawlers['인천'].orders == [] and 'guard' in trace['discarded']


def test_deadline_terminal_update_race_cannot_overwrite_new_owner(env, monkeypatch):
    sc, pool, db, alerts = env
    uo, mid, creds = setup(env, suppliers=(('인천','P1'),('백제','P1')))
    clock = [0]; monkeypatch.setattr(sch.time, 'monotonic', lambda: clock[0])
    sc._crawlers = {'인천': make_crawler(4), '백제': make_crawler(before_search=lambda _: clock.__setitem__(0,601))}
    owned_update = sch._owned_update
    def race(conn, cur, target, token, sql, params=()):
        if '"completedAt"=CASE' in sql:
            update(db, uo, '"sendingToken"=%s,"checkRevision"=7', ('new-owner',))
        return owned_update(conn, cur, target, token, sql, params)
    monkeypatch.setattr(sch, '_owned_update', race)
    result = run(env, uo, creds)
    assert result.lost and result.filled == 4
    assert read_urgent(db, uo) == (4, False, False, 'new-owner', False)
    with db.connection() as conn, conn.cursor() as cur:
        cur.execute('SELECT "checkRevision" FROM domae_urgent_orders WHERE id=%s', (uo,))
        assert cur.fetchone() == (7,)
