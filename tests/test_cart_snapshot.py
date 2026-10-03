# tests/test_cart_snapshot.py
import sys
sys.path.insert(0, "src")

import fakeredis
import pytest

import domae_mcp.core.crawlers.cart_snapshot as cs
from domae_mcp.core.crawlers.cart_snapshot import CartSnapshot
from domae_mcp.core.crawlers.base import OrderResult, confirmed_quantity


@pytest.fixture(autouse=True)
def suppress_external_notifications(monkeypatch):
    monkeypatch.setattr(cs, "_notify", lambda *a, **k: None)


def test_restore_failed_is_recorded_and_notified(monkeypatch):
    sent = []
    monkeypatch.setattr(cs, "_notify", lambda monitor, text, reply_markup=None: sent.append((text, reply_markup)))
    r = fakeredis.FakeRedis()
    s = CartSnapshot(r, "m", "백제")
    s.lock()
    s.save({"Z": 3})
    s.restore_failed("복원 실패")
    assert r.get(s.failed_key) and "복원 실패" in sent[0][0]
    assert sent[0][1]["inline_keyboard"][0][0]["callback_data"].startswith("CR:")


def test_notify_failure_does_not_raise(monkeypatch):
    def boom(*a):
        raise RuntimeError("telegram down")
    monkeypatch.setattr(cs, "_notify", boom)
    s = CartSnapshot(fakeredis.FakeRedis(), "m", "백제")
    s.lock()
    s.save({"Z": 3})
    s.restore_failed("x")


class FakeCart(cs.CartGuardMixin):
    SUPPLIER_NAME = "fake"

    def __init__(self, cart, store):
        self.cart, self.cart_snapshot, self.cleared = dict(cart), store, 0
        self.on_add = None

    def _cart_map(self):
        return dict(self.cart)

    def _cart_clear_raw(self):
        self.cart, self.cleared = {}, self.cleared + 1

    def _cart_add_raw(self, k, q):
        self.cart[k] = self.cart.get(k, 0) + q
        if self.on_add:
            self.on_add(self, k)


def _store(r=None):
    return CartSnapshot(r or fakeredis.FakeRedis(), "m", "백제", account="acct")


def _to_sent(c, ours):
    err, snap = c._cart_start()
    assert err is None
    c._cart_build(snap, ours)
    return snap


def test_normal_restore_after_accept():
    c = FakeCart({"Z": 3}, _store())
    snap = _to_sent(c, {"A": 2})
    c.cart = {}                                                   # 접수되어 비워짐
    assert c._cart_finish(snap, [{}, {"A": 2}]) and c.cart == {"Z": 3}
    assert c.cart_snapshot.load() is None and not c.cart_snapshot._r.get(c.cart_snapshot.lock_key)


@pytest.mark.parametrize("changed", [{"A": 8}, {"A": 1}, {"X": 1}, {"A": 2, "X": 1}, {"Z": 1}])
def test_any_other_state_is_not_touched(changed):
    c = FakeCart({"Z": 3}, _store())
    snap = _to_sent(c, {"A": 2})
    c.cart = dict(changed)
    assert not c._cart_finish(snap, [{}, {"A": 2}]) and c.cart == changed
    assert c.cart_snapshot.load() is not None and c.cart_snapshot._r.get(c.cart_snapshot.failed_key)


@pytest.mark.parametrize("user_state", [{"Z": 1}, {}, {"A": 2}])
def test_change_before_touch_is_left_alone(user_state):
    """비우기 전에 약사가 바꾸면(감량·전부 삭제·요청과 우연히 같은 상태) 그냥 그만둔다 — 원래로 되돌리지 않는다."""
    c = FakeCart({"Z": 3}, _store())
    err, snap = c._cart_start()
    c.cart = dict(user_state)
    with pytest.raises(cs.CartChanged):
        c._cart_build(snap, {"A": 2})
    assert c._cart_finish(snap, [{}, {"A": 2}]) and c.cart == user_state and c.cleared == 0
    assert c.cart_snapshot.load() is None                         # 건드리지 않았으므로 막지 않음


def test_frozen_after_detection_blocks_restore():
    c = FakeCart({"Z": 3}, _store())
    snap = _to_sent(c, {"A": 2})
    c.cart = {"A": 3}
    with pytest.raises(cs.CartChanged):
        c._cart_check({"A": 2})                                   # 전송 직전 대조에서 감지
    c.cart = {}                                                   # 그 뒤 우연히 허용 상태가 되어도
    assert not c._cart_finish(snap, [{}, {"A": 2}]) and c.cart == {}


def test_change_during_restore_stops_immediately():
    c = FakeCart({"Z": 3, "X": 2}, _store())
    snap = _to_sent(c, {"A": 2})
    c.cart = {}

    def user_adds(fc, k):
        if k == "Z":
            fc.cart["X"] = 7                                      # 복원 중 약사가 X 를 담음
    c.on_add = user_adds
    assert not c._cart_finish(snap, [{}, {"A": 2}]) and c.cart == {"Z": 3, "X": 7}


def test_lost_lock_stops_mutation():
    r = fakeredis.FakeRedis()
    c = FakeCart({"Z": 3}, _store(r))
    snap = _to_sent(c, {"A": 2})
    r.delete(c.cart_snapshot.lock_key)                           # 만료
    other = _store(r)
    assert other.lock()                                           # 다른 실행이 잠금을 잡음
    c.cart = {}
    assert not c._cart_finish(snap, [{}, {"A": 2}]) and c.cart == {}
    assert r.get(other.lock_key).decode() == other.run_id        # 남의 잠금을 풀지 않음


def test_crash_leftover_blocks_unless_cart_is_original(monkeypatch):
    sent = []
    monkeypatch.setattr(cs, "_notify", lambda m, t, reply_markup=None: sent.append(reply_markup))
    r = fakeredis.FakeRedis()
    c = FakeCart({"Z": 3}, _store(r))
    _to_sent(c, {"A": 2})                                         # 워커가 죽음
    r.delete(c.cart_snapshot.lock_key)
    c2 = FakeCart(c.cart, _store(r))
    assert c2._cart_start()[0] is not None and c2.cleared == 0 and sent    # 자동 복구 없음, 버튼 재발급
    assert r.ttl(c.cart_snapshot.key) == -1                       # 기록은 만료되지 않음
    c3 = FakeCart({"Z": 3}, _store(r))                            # 약사가 원래대로 되돌려 둠
    assert c3._cart_start()[0] is None


def test_live_run_blocks_second_run():
    r = fakeredis.FakeRedis()
    c = FakeCart({"Z": 3}, _store(r))
    _to_sent(c, {"A": 2})
    c2 = FakeCart(c.cart, _store(r))
    assert c2._cart_start()[0] is not None and c2.cart == {"A": 2}


def test_release_button_rules():
    r = fakeredis.FakeRedis()
    c = FakeCart({"Z": 3}, _store(r))
    snap = _to_sent(c, {"A": 2})
    c.cart = {"X": 1}
    c._cart_finish(snap, [{}, {"A": 2}])
    rev = c.cart_snapshot.load()["rev"]
    s2 = _store(r)
    assert s2.release(rev + 1) != "ok"                            # 예전 버튼
    s2.lock()
    assert s2.release(rev) != "ok"                                # 실행 중
    s2.unlock()
    assert s2.release(rev) == "ok" and s2.load() is None and not r.get(s2.failed_key)


def test_old_run_cannot_touch_new_record():
    r = fakeredis.FakeRedis()
    old = FakeCart({"Z": 3}, _store(r))
    _to_sent(old, {"A": 2})
    r.delete(old.cart_snapshot.key)                               # 해제됨
    r.delete(old.cart_snapshot.lock_key)
    new = FakeCart({"Q": 1}, _store(r))
    _, nsnap = new._cart_start()
    old.cart_snapshot.done()                                      # 오래된 실행의 done·실패 기록은 무시
    old.cart_snapshot.restore_failed("옛 실패")
    assert new.cart_snapshot.load()["run"] == new.cart_snapshot.run_id and not r.get(new.cart_snapshot.failed_key)


def test_restore_detects_failed_delete():
    c = FakeCart({}, _store())
    snap = _to_sent(c, {"A": 2})
    c._cart_clear_raw = lambda: None                              # 삭제가 실제로 안 됨
    assert not c._cart_finish(snap, [{}, {"A": 2}]) and c.cart == {"A": 2}


def test_done_clears_failed_marker_of_same_rev():
    r = fakeredis.FakeRedis()
    c = FakeCart({"Z": 3}, _store(r))
    snap = _to_sent(c, {"A": 2})
    c.cart_snapshot.restore_failed("일시 실패")
    c.cart = {}
    assert c._cart_finish(snap, [{}, {"A": 2}]) and not r.get(c.cart_snapshot.failed_key)


def test_confirmed_quantity():
    assert confirmed_quantity(OrderResult(success=True), 5) == 5
    assert confirmed_quantity(OrderResult(success=True, adjusted_quantity=2), 5) == 2
    assert confirmed_quantity(OrderResult(success=False, reason_code="send_unknown", fulfilled_quantity=3), 5) == 3
    assert confirmed_quantity(OrderResult(success=False, reason_code="send_unknown", fulfilled_quantity=9), 5) is None
    assert confirmed_quantity(OrderResult(success=False, reason_code="not_sent"), 5) is None

def test_done_requires_live_owner():
    r = fakeredis.FakeRedis()
    s = _store(r)
    assert s.lock()
    s.save({'Z': 3})
    r.set(s.lock_key, 'replacement')
    s.done()
    assert s.load() is not None


def test_each_delete_checks_immediately_before_touch():
    class Deleting(FakeCart):
        def _cart_delete_raw(self, key):
            del self.cart[key]
            self.deleted.append(key)
        def _cart_check(self, state):
            super()._cart_check(state)
            self.checks += 1
            if self.checks == 3:
                self.cart['USER'] = 4
    c = Deleting({'Z': 3, 'X': 2}, _store())
    c.deleted, c.checks = [], 0
    _, snap = c._cart_start()
    with pytest.raises(cs.CartChanged):
        c._cart_build(snap, {})
    assert c.deleted == ['Z'] and c.cart['X'] == 2


def test_read_failure_freezes_future_mutations():
    c = FakeCart({'Z': 3}, _store())
    _, snap = c._cart_start()
    read = c._cart_map
    c._cart_map = lambda: (_ for _ in ()).throw(ValueError('unreadable'))
    with pytest.raises(Exception):
        c._cart_check(snap)
    c._cart_map = read
    with pytest.raises(cs.CartChanged):
        c._cart_build(snap, {'A': 2})
    assert c.cart == snap

@pytest.mark.parametrize('q', [True, -1, 0, '5', 1.5])
def test_confirmed_success_validates_requested(q):
    assert confirmed_quantity(OrderResult(success=True), q) is None

def test_reissue_cannot_notify_replaced_revision(monkeypatch):
    r = fakeredis.FakeRedis()
    s = _store(r)
    s.lock(); s.save({'Z': 3})
    old = s.load()
    s.done(); s.save({'Q': 1})
    sent = []
    monkeypatch.setattr(cs, '_notify', lambda *a, **k: sent.append(a))
    s.reissue(old)
    assert sent == []


def test_lock_renewal_rejects_replaced_record():
    import json
    s = _store()
    s.lock(); s.save({'Z': 3})
    rec = s.load(); rec['rev'] += 1
    s._r.set(s.key, json.dumps(rec))
    assert not s.owned()

@pytest.mark.parametrize('operation', ['save', 'done', 'release', 'restore_failed', 'clear_stale'])
def test_watch_race_never_changes_replacement(operation):
    import json
    r = fakeredis.FakeRedis()
    s = _store(r)
    s.lock()
    if operation != 'save': s.save({'Z': 3})
    if operation == 'release': s.unlock()
    replacement = {'run': 'replacement', 'rev': 99, 'snap': [['Q', 7]]}
    transaction = s._tx
    injected = []
    def racing(fn, *keys):
        def wrap(pipe):
            result = fn(pipe)
            if not injected:
                injected.append(True)
                r.set(s.key, json.dumps(replacement))
                r.set(s.lock_key, 'replacement')
            return result
        return transaction(wrap, *keys)
    s._tx = racing
    try:
        if operation == 'save': s.save({'Z': 3})
        elif operation == 'release': assert s.release(s.rev) != 'ok'
        elif operation == 'restore_failed': s.restore_failed('old')
        elif operation == 'clear_stale': s.clear_stale(s.rev)
        else: s.done()
    except cs.CartChanged:
        assert operation in ('save', 'clear_stale')
    assert s.load() == replacement and r.get(s.lock_key) == b'replacement'
    assert not r.get(s.failed_key)


def test_same_account_shares_lock_across_monitors_and_records_have_no_ttl():
    r = fakeredis.FakeRedis()
    a = CartSnapshot(r, 'monitor-a', '백제', account='same-login')
    b = CartSnapshot(r, 'monitor-b', '백제', account='same-login')
    assert a.lock() and not b.lock()
    a.save({'Z': 3}); a.restore_failed('blocked')
    assert all(r.ttl(k) == -1 for k in (a.key, a.failed_key, a.rev_key))
    assert r.ttl(a.lock_key) > 890

def test_unlock_cannot_release_replaced_revision_even_same_run():
    import json
    s = _store(); s.lock(); s.save({'Z': 3})
    rec = s.load(); rec['rev'] += 1
    s._r.set(s.key, json.dumps(rec))
    s.unlock()
    assert s._r.get(s.lock_key) is not None

def test_failed_notice_and_blocked_reissue_share_hourly_revision_limit(monkeypatch):
    sent = []
    monkeypatch.setattr(cs, '_notify', lambda *a, **k: sent.append(a))
    r = fakeredis.FakeRedis()
    s = _store(r); s.lock(); s.save({'Z': 3}); s.restore_failed('failed'); s.unlock()
    next_run = _store(r); next_run.lock(); next_run.reissue(next_run.load())
    assert len(sent) == 1
    r.delete(next_run.reissue_key)
    next_run.reissue(next_run.load())
    assert len(sent) == 2

def test_redis_check_failure_freezes_run(monkeypatch):
    c = FakeCart({'Z': 3}, _store()); _, snap = c._cart_start()
    owned = c.cart_snapshot.owned
    monkeypatch.setattr(c.cart_snapshot, 'owned', lambda: (_ for _ in ()).throw(ConnectionError('redis lost')))
    with pytest.raises(ConnectionError): c._cart_check(snap)
    monkeypatch.setattr(c.cart_snapshot, 'owned', owned)
    with pytest.raises(cs.CartChanged): c._cart_build(snap, {'A': 2})
    assert c.cart == snap

@pytest.mark.parametrize("supplier", ["티제이팜", "백제", "지오영"])
def test_confirmation_button_binds_account_and_fits_telegram(monkeypatch, supplier):
    sent = []
    monkeypatch.setattr(cs, '_notify', lambda *a, **k: sent.append(a))
    store = CartSnapshot(fakeredis.FakeRedis(), 'monitor1full', supplier, account='private-login')
    store.lock(); store.save({}); store.restore_failed('blocked')
    data = sent[0][2]['inline_keyboard'][0][0]['callback_data']
    assert len(data.split(':')) == 5 and len(data.encode()) <= 64
    assert 'private-login' not in data
    assert data.split(':')[-1] == store.account_binding
    store._send_button(9007199254740991, 'largest revision', {})
    assert len(sent[-1][2]['inline_keyboard'][0][0]['callback_data'].encode()) <= 64
    original = data
    store.unlock(); store._r.delete(store.reissue_key)
    resumed = CartSnapshot(store._r, 'monitor1full', supplier, account='private-login')
    resumed.lock(); resumed.reissue(resumed.load())
    assert sent[-1][2]['inline_keyboard'][0][0]['callback_data'] == original


@pytest.mark.parametrize('mutation, lose_on_read', [('delete', 2), ('clear', 1), ('add', 2)])
@pytest.mark.parametrize('replacement', [None, 'foreign-owner'])
def test_lease_loss_during_before_mutation_read_freezes_without_touch(mutation, lose_on_read, replacement):
    class ReadingCart(FakeCart):
        def __init__(self, cart, store):
            super().__init__(cart, store)
            self.reads, self.mutations, self.on_read = 0, [], None

        def _cart_map(self):
            self.reads += 1
            if self.on_read:
                self.on_read(self.reads)
            return super()._cart_map()

        def _cart_clear_raw(self):
            self.mutations.append('clear')
            super()._cart_clear_raw()

        def _cart_add_raw(self, key, quantity):
            self.mutations.append('add')
            super()._cart_add_raw(key, quantity)

    class DeletingCart(ReadingCart):
        def _cart_delete_raw(self, key):
            self.mutations.append('delete')
            del self.cart[key]

    store = _store()
    initial = {} if mutation == 'add' else {'A': 2}
    assert store.lock()
    store.save(initial)
    receipt_key = store.release_key(store.rev)
    store.unlock()
    assert store.release(store.rev) == 'ok'
    cart = (DeletingCart if mutation == 'delete' else ReadingCart)(initial, store)
    error, snap = cart._cart_start()
    assert error is None
    # Existing durable evidence must survive a lost owner, including finish/unlock.
    store.restore_failed('existing failure')
    evidence = {key: store._r.get(key) for key in (store.key, store.failed_key, receipt_key)}
    cart.reads = 0

    def lose_lease(read):
        if read == lose_on_read:
            if replacement is None:
                store._r.delete(store.lock_key)
            else:
                store._r.set(store.lock_key, replacement, ex=17)
    cart.on_read = lose_lease
    target = {'A': 2} if mutation == 'add' else {}
    with pytest.raises(cs.CartChanged, match='잠금'):
        cart._cart_build(snap, target)
    assert cart._cart_frozen and not cart._cart_touched
    assert cart.cart == initial and cart.mutations == []
    reads = cart.reads
    with pytest.raises(cs.CartChanged):
        cart._cart_build(snap, target)
    cart._cart_finish(snap, [target])
    assert cart.reads == reads and cart.mutations == []
    assert {key: store._r.get(key) for key in evidence} == evidence
    assert store._r.get(store.lock_key) == (replacement.encode() if replacement else None)


def test_same_owner_read_renews_lease_after_network_boundary():
    class ReadingCart(FakeCart):
        def _cart_map(self):
            self.cart_snapshot._r.expire(self.cart_snapshot.lock_key, 1)
            return super()._cart_map()

    store = _store()
    assert store.lock()
    store.save({'A': 2})
    cart = ReadingCart({'A': 2}, store)
    cart._cart_check({'A': 2})
    assert not cart._cart_frozen and not cart._cart_touched
    assert store._r.get(store.lock_key).decode() == store.run_id
    assert store._r.ttl(store.lock_key) > cs.LOCK_TTL - 5
