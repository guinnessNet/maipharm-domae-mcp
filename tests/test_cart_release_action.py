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
           'supplier': '백제', 'revision': rev+1 if mode == 'stale' else rev, 'chat_id': '123456'}
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
    cur.fetchone.side_effect = [('monitor1full', {}), None]
    pool = Mock()
    scheduler = CloudScheduler(pool, fakeredis.FakeRedis())
    scheduler._get_conn = lambda: conn
    scheduler._decrypt_creds = lambda c: {'백제': {'login_id': 'login'}}
    scheduler._crawlers_loaded = True
    class Crawler:
        def login(self, *a): return True
        def search(self, *a): return []
        def order(self, *a, **kw): return OrderResult(success=False, reason_code='send_unknown', fulfilled_quantity=3)
    scheduler._crawlers = {'백제': Crawler}
    monkeypatch.setattr(Notifier, 'send_order_result', lambda *a, **k: None)
    scheduler.telegram_order({'monitor_prefix': 'monitor1', 'supplier': '백제', 'product_id': 'p', 'quantity': 5, 'chat_id': '12345'})
    sql, args = next(c.args for c in cur.execute.call_args_list if 'INSERT INTO domae_cloud_orders' in c.args[0])
    fields = [field.strip().strip('"') for field in sql[sql.index('(')+1:sql.index(')')].split(',')]
    row = dict(zip(fields, args))
    assert row['success'] is None and row['confirmedQuantity'] == 3 and row['reasonCode'] == 'send_unknown'

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
