import json
import sys
from unittest.mock import Mock
sys.path.insert(0, 'src')
import fakeredis
import pytest
from domae_mcp.cloud.scheduler import CloudScheduler
from domae_mcp.cloud.worker import CloudWorker
from domae_mcp.cloud.notifier import Notifier
from domae_mcp.core.crawlers.cart_snapshot import CartSnapshot

@pytest.mark.parametrize('mode', ['ok', 'foreign', 'stale', 'live', 'repeat'])
def test_release_action_revalidates_and_audits(monkeypatch, mode):
    redis = fakeredis.FakeRedis(decode_responses=True)
    store = CartSnapshot(redis, 'monitor1full', '백제', account='login')
    store.lock(); rev = store.save({'Z': 3}); store.unlock()
    if mode == 'live': store.lock()
    conn, cur = Mock(), Mock()
    conn.cursor.return_value = cur
    cur.fetchone.return_value = None if mode == 'foreign' else ('monitor1full', {})
    pool = Mock(); pool.getconn.return_value = conn
    scheduler = CloudScheduler(pool, redis)
    scheduler._decrypt_creds = lambda c: {'백제': {'login_id': 'login'}}
    sent = []
    monkeypatch.setattr(Notifier, 'send_telegram', lambda *a, **k: sent.append(a))
    job = {'action': 'cart_release', 'monitor_id': 'monitor1full', 'monitor_prefix': 'monitor1',
           'supplier': '백제', 'account_binding': store.account_binding, 'revision': rev+1 if mode == 'stale' else rev, 'chat_id': '123456'}
    scheduler.cart_release(job)
    if mode == 'repeat': scheduler.cart_release(job)
    audits = [args for args, kw in cur.execute.call_args_list if 'INSERT INTO domae_order_audit_events' in args[0]]
    assert len(audits) == (1 if mode in ('ok', 'repeat') else 0)
    assert (store.load() is None) == (mode in ('ok', 'repeat'))
    assert pool.putconn.call_count == (2 if mode == 'repeat' else 1)
    assert sent
    assert redis.zcard("domae:cart_release_pending") == 0
    query = next(call.args for call in cur.execute.call_args_list if "telegramChatId" in call.args[0])
    assert '"telegramChatId" = %s' in query[0] and '123456' in query[1]


def test_worker_dispatches_cart_release(monkeypatch):
    worker = CloudWorker.__new__(CloudWorker)
    worker._running = True
    worker._redis = Mock()
    worker._scheduler, worker._db_pool, worker._executor = Mock(), Mock(), Mock()
    worker._drain_delayed = lambda: None
    job = {'action': 'cart_release', 'revision': 2}
    def take(*a, **k):
        worker._running = False
        return 'domae:jobs:urgent', json.dumps(job)
    worker._redis.brpop.side_effect = take
    worker.run()
    worker._scheduler.cart_release.assert_called_once_with(job)
    worker._scheduler.recover_cart_releases.assert_called_once_with()

def test_telegram_order_preserves_unknown_partial_quantity(monkeypatch):
    from domae_mcp.core.crawlers.base import OrderResult
    conn, cur = Mock(), Mock()
    conn.cursor.return_value = cur
    cur.fetchone.side_effect = [('monitor1full', {}), None, ('attempt-row',)]
    pool = Mock()
    scheduler = CloudScheduler(pool, fakeredis.FakeRedis())
    scheduler._get_conn = lambda: conn
    scheduler._decrypt_creds = lambda c: {'백제': {'login_id': 'login'}}
    scheduler._crawlers_loaded = True
    class Crawler:
        def login(self, *a): return True
        def search(self, *a): return []
        def order(self, *a, **kw):
            conn.commit.assert_called_once()  # pending marker committed before send
            return OrderResult(success=False, reason_code='send_unknown', fulfilled_quantity=3)
    scheduler._crawlers = {'백제': Crawler}
    monkeypatch.setattr(Notifier, 'send_order_result', lambda *a, **k: None)
    scheduler.telegram_order({'monitor_prefix': 'monitor1', 'supplier': '백제', 'product_id': 'p', 'quantity': 5, 'chat_id': '12345'})
    sql, args = next(c.args for c in cur.execute.call_args_list if 'INSERT INTO domae_cloud_orders' in c.args[0])
    assert "NULL" in sql and "'send_unknown'" in sql and 'ON CONFLICT' in sql
    pending_id = args[0]
    sql, args = next(c.args for c in cur.execute.call_args_list if 'UPDATE domae_cloud_orders' in c.args[0])
    assert args[:3] == (None, 'send_unknown', 3) and args[-1] == pending_id
    assert conn.commit.call_count == 2

def test_recovery_is_bounded_and_retains_receipt_on_failed_commit():
    redis = fakeredis.FakeRedis()
    for i in range(12):
        store = CartSnapshot(redis, 'monitor1full', '백제', account=f'account{i}')
        store.lock(); rev = store.save({}); store.unlock()
        assert store.release(rev) == 'ok'
    conn, cur, pool = Mock(), Mock(), Mock()
    conn.cursor.return_value = cur; pool.getconn.return_value = conn
    conn.commit.side_effect = RuntimeError('DB unavailable')
    scheduler = CloudScheduler(pool, redis)
    scheduler.recover_cart_releases()
    assert conn.commit.call_count == 10
    assert redis.zcard('domae:cart_release_pending') == 12
    for key in redis.zrange('domae:cart_release_pending', 0, -1):
        assert redis.ttl(key) == -1
    conn.commit.side_effect = None
    scheduler.recover_cart_releases()
    assert redis.zcard('domae:cart_release_pending') == 2
    scheduler.recover_cart_releases()
    assert redis.zcard('domae:cart_release_pending') == 0


def test_release_receipt_ack_cannot_delete_replacement():
    redis = fakeredis.FakeRedis()
    store = CartSnapshot(redis, 'monitor1full', '백제', account='account')
    store.lock(); rev = store.save({}); store.unlock(); store.release(rev)
    key = store.release_key(rev)
    receipt = store.release_receipt(redis, key)
    replacement = {**receipt, 'id': 'replacement'}
    redis.set(key, json.dumps(replacement))
    assert not CartSnapshot.ack_release(redis, key, receipt)
    assert store.release_receipt(redis, key) == replacement
    assert redis.zcard('domae:cart_release_pending') == 1

def test_interruption_before_release_transaction_exec_keeps_snapshot_and_no_receipt():
    redis = fakeredis.FakeRedis()
    store = CartSnapshot(redis, 'monitor1full', '백제', account='account')
    store.lock(); rev = store.save({'original': 3}); store.unlock()
    original = store.load()
    tx = store._tx
    def interrupt_before_exec(fn, *keys):
        def call(pipe):
            fn(pipe)
            raise KeyboardInterrupt('before EXEC')
        return tx(call, *keys)
    store._tx = interrupt_before_exec
    with pytest.raises(KeyboardInterrupt): store.release(rev)
    assert store.load() == original
    assert not redis.get(store.release_key(rev))
    assert redis.zcard('domae:cart_release_pending') == 0

def test_permanent_first_ten_failures_do_not_starve_eleventh_receipt():
    redis = fakeredis.FakeRedis()
    healthy_id = None
    for i in range(11):
        store = CartSnapshot(redis, f'monitor{i}', '백제', account=f'fair{i}')
        store.lock(); rev = store.save({}); store.unlock(); store.release(rev)
        if i == 10: healthy_id = store.release_receipt(redis, store.release_key(rev))['id']
    conn, cur, pool = Mock(), Mock(), Mock()
    conn.cursor.return_value = cur; pool.getconn.return_value = conn
    attempted = []
    def execute(sql, args=None):
        if 'INSERT INTO domae_order_audit_events' in sql:
            attempted.append(args[0])
            if args[0] != healthy_id: raise RuntimeError('per-receipt permanent failure')
    cur.execute.side_effect = execute
    scheduler = CloudScheduler(pool, redis)
    for _ in range(3):
        before = len(attempted)
        scheduler.recover_cart_releases()
        assert len(attempted) - before <= 10
    assert healthy_id in attempted
    assert redis.zcard('domae:cart_release_pending') == 10

@pytest.mark.parametrize('failure', ['parse', 'rollback', 'poolreturn'])
def test_recovery_failure_does_not_block_normal_dequeue(monkeypatch, failure):
    redis = fakeredis.FakeRedis()
    store = CartSnapshot(redis, 'monitor1', '백제', account='broken')
    store.lock(); rev = store.save({}); store.unlock(); store.release(rev)
    key = store.release_key(rev)
    if failure == 'parse': redis.set(key, 'invalid-json')
    conn, cur, pool = Mock(), Mock(), Mock()
    conn.cursor.return_value = cur; pool.getconn.return_value = conn
    cur.execute.side_effect = lambda sql, args=None: (_ for _ in ()).throw(RuntimeError('DB lost')) if 'INSERT INTO domae_order_audit_events' in sql else None
    if failure == 'rollback': conn.rollback.side_effect = RuntimeError('rollback disconnected')
    if failure == 'poolreturn': pool.putconn.side_effect = RuntimeError('pool return disconnected')
    scheduler = CloudScheduler(pool, redis)
    scheduler.execute = Mock()
    worker = CloudWorker.__new__(CloudWorker)
    worker._running = True; worker._redis = redis; worker._scheduler = scheduler
    worker._db_pool = pool; worker._executor = Mock(); worker._drain_delayed = lambda: None
    def take(*a, **k):
        worker._running = False
        return 'domae:jobs', json.dumps({'action': 'monitor', 'monitor_id': 'm'})
    redis.brpop = take
    # Stop on error to avoid an infinite test run when the pre-fix worker skips dequeue.
    monkeypatch.setattr('domae_mcp.cloud.worker.time.sleep', lambda *_: setattr(worker, '_running', False))
    scheduler.recover_cart_releases()  # must contain per-receipt failures itself
    worker.run()
    scheduler.execute.assert_called_once()
    assert redis.get(key) and redis.zcard('domae:cart_release_pending') == 1
    if failure == 'rollback': pool.putconn.assert_any_call(conn, close=True)
    if failure == 'poolreturn': assert conn.close.call_count == 2

def test_unexpected_recovery_exception_still_dequeues_job():
    worker = CloudWorker.__new__(CloudWorker)
    worker._running = True
    worker._redis, worker._scheduler, worker._db_pool, worker._executor = Mock(), Mock(), Mock(), Mock()
    worker._drain_delayed = lambda: None
    def fail():
        worker._running = False
        raise RuntimeError('unexpected recovery failure')
    worker._scheduler.recover_cart_releases.side_effect = fail
    job = {'action': 'monitor', 'monitor_id': 'm'}
    worker._redis.brpop.return_value = 'domae:jobs', json.dumps(job)
    worker.run()
    worker._scheduler.execute.assert_called_once_with(job)

@pytest.mark.parametrize('binding_kind', ['old_account', 'malformed', 'forged', 'legacy'])
def test_old_account_callback_cannot_release_new_accounts_same_revision(monkeypatch, binding_kind):
    import base64, hashlib
    redis = fakeredis.FakeRedis()
    old = CartSnapshot(redis, 'monitor1full', '백제', account='account-A')
    current = CartSnapshot(redis, 'monitor1full', '백제', account='account-B')
    for store in (old, current):
        store.lock(); assert store.save({'original': 3}) == 1; store.unlock()
    conn, cur, pool = Mock(), Mock(), Mock()
    conn.cursor.return_value = cur; cur.fetchone.return_value = ('monitor1full', {})
    pool.getconn.return_value = conn
    scheduler = CloudScheduler(pool, redis)
    scheduler._decrypt_creds = lambda c: {'백제': {'login_id': 'account-B'}}
    monkeypatch.setattr(Notifier, 'send_telegram', lambda *a, **k: None)
    job = {'monitor_id': 'monitor1full', 'monitor_prefix': 'monitor1', 'supplier': '백제', 'revision': 1, 'chat_id': '12345'}
    if binding_kind == 'old_account':
        job['account_binding'] = base64.urlsafe_b64encode(hashlib.sha256(b'account:account-A').digest()[:12]).decode()
    elif binding_kind != 'legacy':
        job['account_binding'] = 'invalid!' if binding_kind == 'malformed' else 'AAAAAAAAAAAAAAAA'
    scheduler.cart_release(job)
    assert old.load() is not None and current.load() is not None
    assert not redis.zcard('domae:cart_release_pending')
    assert not any('INSERT INTO domae_order_audit_events' in c.args[0] for c in cur.execute.call_args_list)
