"""도매 v2 진입점 집행·갱신(gate) 시험 (계획 Task 7). 가짜 서버·가짜 시계·수동 작업 실행기를 쓴다."""
import builtins
import sys
import threading

import pytest

from domae_mcp.core.crawlers.registry import CrawlerBlocked, CrawlerRegistry
from domae_mcp.v2 import gate as gate_mod
from domae_mcp.v2.artifacts import MODULE_PREFIX, unload_all
from domae_mcp.v2.client import V2Client
from domae_mcp.v2.encoding import key_fp_from_bearer
from domae_mcp.v2.gate import RuntimeGate
from domae_mcp.v2.keyslot import KeySlots
from domae_mcp.v2.loader import V2Error, V2Loader
from domae_mcp.v2.runtime import local_runtime_id
from domae_mcp.v2.store import StateStore
from v2_fakes import (DEVICE_HEX, TEST_BEARER, TEST_BEARER_2, TEST_TRUST, FakeTime, FakeV2Server, H, T0,
                      compile_artifact, fake_artifacts, pick_protector)

FA, FB = key_fp_from_bearer(TEST_BEARER), key_fp_from_bearer(TEST_BEARER_2)


class FakeWorker:
    def __init__(self):
        self.alive = True

    def is_alive(self):
        return self.alive


class Jobs:
    """수동 실행기: require() 가 시작한 작업을 시험이 원할 때 돌린다."""

    def __init__(self):
        self.pending = []

    def __call__(self, fn):
        w = FakeWorker()
        self.pending.append((fn, w))
        return w

    def run_all(self):
        while self.pending:
            fn, w = self.pending.pop(0)
            try:
                fn()
            finally:
                w.alive = False


class Env:
    def __init__(self, root):
        self.root = root / "v2"
        self.t = FakeTime(T0)
        self.prot = pick_protector()
        self.plain = fake_artifacts()
        self.server = FakeV2Server(self.plain, now=lambda: self.t.w)
        self.jobs = Jobs()
        self.loaders = []
        self.new_process()

    def factory(self, bearer, still_current, policy_write):
        http = self.server.client()
        ld = V2Loader(bearer, self.slots.cache_dir(key_fp_from_bearer(bearer)), self.state, self.prot,
                      client=V2Client(bearer, DEVICE_HEX, http=http, base="https://pharmsq.com/api/domae"),
                      clock=self.t.clock(), trust=TEST_TRUST, still_current=still_current,
                      policy_write=policy_write, device_id_hex=DEVICE_HEX, runtime_id=local_runtime_id())
        self.loaders.append(ld)
        return ld

    def new_process(self):
        """exe 재시작 흉내: 저장소·게이트를 새로 만들고 레지스트리·모듈을 비운다."""
        CrawlerRegistry.clear()
        unload_all()
        self.state = StateStore(self.root, self.prot)
        self.slots = KeySlots(self.root, self.prot)
        self.gate = RuntimeGate(self.slots, self.state, self.prot, clock=self.t.clock(),
                                loader_factory=self.factory, runner=self.jobs)
        self.gate.install_guard()
        return self.gate

    def first_key(self, bearer=TEST_BEARER):
        return self.gate.switch(bearer)


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    yield e
    CrawlerRegistry.set_guard(None)
    CrawlerRegistry.clear()
    unload_all()


def _blocked(reason=None):
    with pytest.raises(CrawlerBlocked) as e:
        CrawlerRegistry.require()
    if reason:
        assert e.value.reason == reason
    return e.value.reason


def _mods():
    return {m: sys.modules[m] for m in sys.modules if m.startswith(MODULE_PREFIX)}


# ── 레지스트리 guard ──

def test_no_guard_behaves_as_before():
    CrawlerRegistry.set_guard(None)
    CrawlerRegistry.clear()
    CrawlerRegistry.require()
    assert CrawlerRegistry.list_all() == [] and CrawlerRegistry.is_loaded() is False


def test_blocked_registry_raises_and_is_loaded_false(env):
    assert env.first_key() == 3
    assert CrawlerRegistry.is_loaded() and sorted(CrawlerRegistry.list_all()) == ["가짜A", "가짜B", "가짜C"]
    env.server.mode = "revoke_key"
    env.t.advance(H)
    with pytest.raises(V2Error):
        env.gate.reverify()
    for fn in (CrawlerRegistry.require, CrawlerRegistry.list_all, CrawlerRegistry.get_all,
               lambda: CrawlerRegistry.get("가짜A")):
        with pytest.raises(CrawlerBlocked):
            fn()
    assert CrawlerRegistry.is_loaded() is False and _mods() == {}


def test_no_key_is_not_loaded(env):
    env.gate.start()
    _blocked("not_loaded")
    assert env.gate.status()["state"] == "no_key" and env.jobs.pending == []


# ── 실행 중 집행 ──

def test_offline_deadline_blocks_running_process(env):
    env.first_key()
    env.server.version_status = (503, {"error": "no_release"}, None)
    env.server.post_status = (503, {"error": "no_release"}, None)
    env.t.advance(144 * H)
    _blocked("offline_deadline")


def test_running_clock_regression_blocks(env):
    env.first_key()
    env.t.advance(H)
    CrawlerRegistry.require()  # high water 기록
    env.t.advance(-60)
    _blocked("clock_untrusted")


def test_downgrade_policy_raised_while_running_blocks(env):
    env.first_key()
    env.state.update(FA, lambda s: (setattr(s, "min_release_seq", 2), s)[1])
    _blocked("downgrade")


# ── 갱신 ──

def test_renewal_starts_once_after_expires(env):
    env.first_key()
    env.t.advance(72 * H)
    CrawlerRegistry.require()
    CrawlerRegistry.require()
    assert len(env.jobs.pending) == 1


def test_renewal_failure_keeps_serving_until_deadline(env):
    env.first_key()
    env.server.version_status = (503, {"error": "no_release"}, None)
    env.t.advance(72 * H)
    CrawlerRegistry.require()
    env.jobs.run_all()
    assert env.gate.status()["version_check_failed"] is True
    env.t.advance(71 * H)
    CrawlerRegistry.require()
    env.jobs.run_all()
    env.t.advance(H)
    _blocked("offline_deadline")


def test_renewal_revocation_blocks(env):
    env.first_key()
    env.server.mode = "revoke_device"
    env.t.advance(72 * H)
    CrawlerRegistry.require()
    env.jobs.run_all()
    _blocked("device_revoked")
    assert env.slots.cache_dir(FA).joinpath("bundle-cache.json").exists() is False


def test_renewal_success_swaps_registry(env):
    env.first_key()
    old = CrawlerRegistry._crawlers["가짜A"]
    env.server.publish()
    env.t.advance(72 * H)
    CrawlerRegistry.require()
    env.jobs.run_all()
    assert env.gate.status()["release_seq"] == 2
    assert CrawlerRegistry._crawlers["가짜A"] is not old
    assert env.server.posts == 2


def test_require_does_not_block_on_network(env):
    env.first_key()
    real = []
    env.gate._runner = lambda fn: (real.append(fn), FakeWorker())[1]  # 작업을 돌리지 않는다
    env.t.advance(72 * H)
    gets = env.server.gets
    CrawlerRegistry.require()
    assert env.server.gets == gets and len(real) == 1


# ── 전환 ──

def test_switch_commits_only_after_full_load(env):
    env.first_key()
    bad = dict(env.plain)
    bad["fake_gamma"] = compile_artifact("raise RuntimeError('x')", "fake_gamma")
    env.server.plain = bad
    with pytest.raises(V2Error) as e:
        env.gate.switch(TEST_BEARER_2)
    assert e.value.reason == "load"
    assert env.slots.active()[0] == FA
    CrawlerRegistry.require()
    assert sorted(CrawlerRegistry.list_all()) == ["가짜A", "가짜B", "가짜C"]


def test_commit_failure_rolls_back_modules(env, monkeypatch):
    env.first_key()
    before = _mods()
    reg = dict(CrawlerRegistry._crawlers)

    def boom(fp):
        raise OSError("disk")

    monkeypatch.setattr(env.slots, "commit", boom)
    with pytest.raises(OSError):
        env.gate.switch(TEST_BEARER_2)
    assert _mods() == before and all(_mods()[k] is before[k] for k in before)
    assert CrawlerRegistry._crawlers == reg
    assert env.slots.active()[0] == FA
    CrawlerRegistry.require()


def test_switch_success_forgets_old_key(env):
    env.first_key()
    env.gate.switch(TEST_BEARER_2)
    assert env.slots.active()[0] == FB
    assert not env.state.has(FA) and not env.slots.cache_dir(FA).exists()
    CrawlerRegistry.require()


# ── 세대·backoff ──

def test_publish_rejects_prepared_below_stored_generation(env):
    env.first_key()
    env.new_process()
    ld = env.factory(TEST_BEARER, lambda: True, None)
    cached = ld.prepare_cached()
    env.server.device_generation = 2
    posts = env.server.posts
    env.gate.start()  # 캐시 publish → version 이 세대 2 저장(즉시 downgrade) → 다운로드·publish 로 해제
    assert env.gate.status()["state"] == "ready" and env.server.posts == posts + 1
    assert env.gate._running.prepared.device_generation == 2
    ctx = env.gate.context()
    assert env.gate.publish(cached, ctx) is False
    assert env.state.get(FA).device_generation == 2


def test_publish_load_failure_sets_backoff(env):
    env.first_key()
    env.server.publish(plain={**env.plain, "fake_gamma": compile_artifact("raise RuntimeError('x')", "fake_gamma")})
    env.t.advance(72 * H)
    CrawlerRegistry.require()
    env.jobs.run_all()
    ks = env.state.get(FA)
    assert ks.backoff_until > env.t.w
    posts = env.server.posts
    env.new_process().start()
    assert env.server.posts == posts


def test_backoff_reset_only_after_network_publish_success(env):
    env.first_key()
    env.state.update(FA, lambda s: (setattr(s, "backoff_level", 3), s)[1])
    env.server.publish()
    env.t.advance(72 * H)
    env.gate.reverify()
    ks = env.state.get(FA)
    assert (ks.backoff_until, ks.backoff_level) == (0, 0)


def test_cache_publish_preserves_backoff_across_restart(env):
    env.first_key()
    env.server.publish()
    env.t.advance(72 * H)  # 캐시 expires_at 지남
    env.server.post_mutate = lambda p: p["modules"]["fake_alpha"].update(tag="A" * 22 + "==")
    CrawlerRegistry.require()
    env.jobs.run_all()  # GCM 실패 → backoff 1
    env.server.post_mutate = None
    env.server.post_status = (429, {"error": "daily_limit"}, {"Retry-After": "7200"})
    env.state.update(FA, lambda s: (setattr(s, "backoff_until", 0), s)[1])
    CrawlerRegistry.require()
    env.jobs.run_all()  # 429 → backoff 2
    level = env.state.get(FA).backoff_level
    until = env.state.get(FA).backoff_until
    assert level == 2 and until >= env.t.w + 7200
    posts = env.server.posts
    env.server.post_status = None
    env.new_process().start()  # exe 재시작: 캐시 publish 성공
    assert env.gate.status()["state"] == "ready"
    CrawlerRegistry.require()
    env.jobs.run_all()
    assert env.server.posts == posts
    ks = env.state.get(FA)
    assert (ks.backoff_level, ks.backoff_until) == (level, until)


def test_generation_raise_blocks_running_bundle_until_new_publish(env):
    env.first_key()
    env.server.device_generation = 1
    env.server.post_status = (503, {"error": "bundle_unavailable"}, None)
    env.t.advance(H)
    with pytest.raises(V2Error):
        env.gate.reverify()
    _blocked("downgrade")
    env.jobs.run_all()
    _blocked("downgrade")
    env.server.post_status = None
    env.t.advance(env.state.get(FA).backoff_until - env.t.w + 1)
    with pytest.raises(CrawlerBlocked):
        CrawlerRegistry.require()
    env.jobs.run_all()
    CrawlerRegistry.require()
    assert env.gate.status()["state"] == "ready"


def test_publish_rejected_has_no_side_effects(env, monkeypatch):
    env.first_key()
    ld = env.factory(TEST_BEARER, lambda: True, None)
    env.t.advance(60)
    old = ld.prepare_cached()
    env.state.update(FA, lambda s: (setattr(s, "device_generation", 5), s)[1])
    ptr = env.root / "active.dpapi"
    ptr_bytes = ptr.read_bytes()
    mods, reg = _mods(), dict(CrawlerRegistry._crawlers)
    execs = {"n": 0}
    real_exec = builtins.exec
    monkeypatch.setattr(builtins, "exec", lambda *a, **k: (execs.__setitem__("n", execs["n"] + 1), real_exec(*a, **k))[1])
    assert env.gate.publish(old, env.gate.context()) is False
    monkeypatch.undo()
    assert execs["n"] == 0 and ptr.read_bytes() == ptr_bytes
    assert all(_mods()[k] is mods[k] for k in mods) and CrawlerRegistry._crawlers == reg


# ── lease ──

def test_renewal_same_release_new_lease_runs_past_first_deadline(env):
    env.first_key()
    env.t.advance(72 * H)
    CrawlerRegistry.require()
    env.jobs.run_all()  # 같은 게시 재발급(새 issued_at)
    env.t.advance(78 * H)  # T0+150H
    CrawlerRegistry.require()


def test_expired_then_online_reissue_recovers(env):
    env.first_key()
    env.server.version_status = (503, {"error": "no_release"}, None)
    env.t.advance(144 * H)
    _blocked("offline_deadline")
    env.jobs.run_all()
    env.server.version_status = None
    env.t.advance(400)  # 버전 확인 재시도 간격(약 300초) 뒤
    _blocked("offline_deadline")
    env.jobs.run_all()
    CrawlerRegistry.require()


# ── 경합 ──

def test_publish_with_stale_context_leaves_sys_modules(env):
    env.first_key()
    ld = env.factory(TEST_BEARER, lambda: True, None)
    env.t.advance(60)
    late_a = ld.prepare_cached()
    ctx_a = env.gate.context()
    env.gate.switch(TEST_BEARER_2)
    mods, reg = _mods(), dict(CrawlerRegistry._crawlers)
    assert env.gate.publish(late_a, ctx_a) is False
    assert all(_mods()[k] is mods[k] for k in mods) and CrawlerRegistry._crawlers == reg
    assert env.slots.active()[0] == FB


def test_switch_during_module_load_serialized(env, monkeypatch):
    env.first_key()
    started, release = threading.Event(), threading.Event()
    real_load = gate_mod.load_modules
    calls = {"n": 0}

    def slow_load(plain, manifest):
        calls["n"] += 1
        if calls["n"] == 1:
            started.set()
            release.wait(5)
        return real_load(plain, manifest)

    monkeypatch.setattr(gate_mod, "load_modules", slow_load)
    env.server.publish()
    env.t.advance(72 * H)
    errors = []

    def run(fn):
        try:
            fn()
        except Exception as e:  # 스레드 예외를 시험이 본다
            errors.append(e)

    t1 = threading.Thread(target=run, args=(env.gate.reverify,))
    t1.start()
    assert started.wait(5)
    t2 = threading.Thread(target=run, args=(lambda: env.gate.switch(TEST_BEARER_2),))
    t2.start()
    t2.join(0.3)
    assert t2.is_alive()  # 게이트 잠금에서 기다린다
    release.set()
    t1.join(5)
    t2.join(5)
    assert not errors
    assert env.slots.active()[0] == FB
    CrawlerRegistry.require()
    assert env.gate.status()["release_seq"] == 2


def test_switch_key_cancels_inflight_refresh(env):
    env.first_key()
    env.t.advance(72 * H)
    CrawlerRegistry.require()
    assert len(env.jobs.pending) == 1
    env.gate.switch_key(FB)
    gets = env.server.gets
    env.jobs.run_all()  # 옛 컨텍스트 작업: 아무것도 하지 않는다
    assert env.server.gets == gets


# ── 시계 복구 ──

def test_clock_untrusted_recovers_on_same_bundle_version_ok(env):
    env.first_key()
    env.t.advance(H)
    CrawlerRegistry.require()
    env.t.advance(-30)
    _blocked("clock_untrusted")
    env.jobs.run_all()
    CrawlerRegistry.require()
    assert env.server.posts == 1


def test_clock_recovery_first_check_fails_then_backoff_retry_recovers(env):
    env.first_key()
    env.t.advance(H)
    CrawlerRegistry.require()
    env.t.advance(-30)
    env.server.version_status = (503, {"error": "no_release"}, None)
    _blocked("clock_untrusted")
    env.jobs.run_all()
    _blocked("clock_untrusted")
    assert env.jobs.pending == []  # backoff 전에는 다시 시작하지 않는다
    env.server.version_status = None
    env.t.advance(301 + 60)
    _blocked("clock_untrusted")
    env.jobs.run_all()
    CrawlerRegistry.require()


def test_min_release_raise_blocks_old_and_downloads(env):
    env.first_key()
    env.server.publish()
    env.server.min_release_seq = 2
    env.t.advance(H)
    env.gate.reverify()
    CrawlerRegistry.require()
    assert env.gate.status()["release_seq"] == 2


def test_min_release_raise_during_refresh_blocks_until_publish(env):
    env.first_key()
    env.server.publish()
    env.server.min_release_seq = 2
    env.server.post_status = (503, {"error": "bundle_unavailable"}, None)
    env.t.advance(72 * H)
    CrawlerRegistry.require()
    env.jobs.run_all()
    _blocked("downgrade")


def test_restart_with_cache_then_revoked_version_blocks(env):
    env.first_key()
    env.server.mode = "revoke_key"
    env.t.advance(60)
    env.new_process().start()
    _blocked("key_revoked")
    assert CrawlerRegistry.is_loaded() is False


def test_restart_uses_cache_without_post(env):
    env.first_key()
    for _ in range(5):
        env.t.advance(60)
        env.new_process().start()
        CrawlerRegistry.require()
    assert env.server.posts == 1


def test_status_fields(env):
    env.first_key()
    s = env.gate.status()
    assert s["state"] == "ready" and s["crawler_count"] == 3 and s["release_seq"] == 1
    assert s["remaining_s"] == 144 * H and s["version_check_failed"] is False
    assert isinstance(s["protected"], bool)


# ── 1차 검수 재현 시험 ──

def test_reverify_post_revocation_blocks_running(env, monkeypatch):
    env.first_key()
    env.server.publish()
    env.t.advance(H)
    real = V2Loader.check_version

    def cv(self):
        v = real(self)
        env.server.mode = "revoke_key"  # 버전 확인은 OK, 이어지는 POST 만 철회
        return v

    monkeypatch.setattr(V2Loader, "check_version", cv)
    with pytest.raises(V2Error):
        env.gate.reverify()
    _blocked("key_revoked")
    assert CrawlerRegistry.is_loaded() is False and _mods() == {}


def test_revocation_recorded_during_switch_still_blocks_running(env):
    env.first_key()
    ctx = env.gate.switch_key(FB)  # 전환이 진행 중
    env.state.update(FA, lambda s: (setattr(s, "revoked", {"scope": "device", "at": T0}), s)[1])
    _blocked("device_revoked")
    env.gate.abort_switch(ctx)
    _blocked("device_revoked")


def test_start_and_concurrent_require_post_once(env, monkeypatch):
    env.first_key()
    env.server.publish()
    env.t.advance(72 * H)
    env.new_process()
    real = V2Loader.prepare_download
    hit = {"n": 0}

    def pd(self):
        hit["n"] += 1
        if hit["n"] == 1:  # 시작 흐름이 다운로드하는 도중 검색이 들어온다
            CrawlerRegistry.require()  # 캐시로 이미 실행 중 — 갱신 작업을 새로 띄우지 않아야 한다
            assert env.jobs.pending == []
            env.jobs.run_all()
        return real(self)

    monkeypatch.setattr(V2Loader, "prepare_download", pd)
    posts = env.server.posts
    env.gate.start()
    assert env.server.posts - posts == 1
    CrawlerRegistry.require()


def test_cancelled_refresh_does_not_set_backoff(env):
    env.first_key()
    env.server.publish()
    env.t.advance(72 * H)

    def cancel_midway(req):
        if req.method == "POST":
            env.gate.switch_key(FB)  # 진행 중 갱신을 취소
            raise RuntimeError("cancelled transport")

    env.server.on_request = cancel_midway
    CrawlerRegistry.require()
    env.jobs.run_all()
    ks = env.state.get(FA)
    assert ks.backoff_level == 0 and ks.backoff_until == 0


def test_server_body_cancelled_still_sets_backoff(env):
    env.first_key()
    env.server.post_status = (503, {"error": "cancelled"}, None)
    env.server.publish()
    env.t.advance(H)
    with pytest.raises(V2Error):
        env.gate.reverify()
    assert env.state.get(FA).backoff_level == 1


def test_download_backoff_suppresses_version_retries(env):
    env.first_key()
    env.server.device_generation = 1
    env.server.post_status = (503, {"error": "bundle_unavailable"}, None)
    env.t.advance(H)
    with pytest.raises(V2Error):
        env.gate.reverify()
    gets = env.server.gets
    for _ in range(5):
        _blocked("downgrade")
        env.jobs.run_all()
    assert env.server.gets == gets  # backoff 동안 버전 확인 GET 을 반복하지 않는다


def test_publish_refuses_older_issuance_same_key(env):
    env.first_key()
    ld = env.factory(TEST_BEARER, lambda: True, None)
    env.t.advance(60)
    old = ld.prepare_cached()
    env.server.publish()
    env.t.advance(H)
    env.gate.reverify()
    assert env.gate.publish(old, env.gate.context()) is False
    assert env.gate.status()["release_seq"] == 2


# ── 결정 A: 서버 시계가 로컬보다 빠른 정상 시차 ──

def test_server_clock_ahead_no_block_no_extra_post(env):
    env.server.now = lambda: env.t.w + 30
    env.first_key()
    for _ in range(10):
        env.t.advance(5)
        CrawlerRegistry.require()
        env.jobs.run_all()
    assert env.server.posts == 1
    env.gate.reverify()
    CrawlerRegistry.require()
    assert env.server.posts == 1


def test_server_clock_ahead_restart_waits_instead_of_post(env):
    env.server.now = lambda: env.t.w + 300
    env.first_key()
    env.t.advance(10)
    env.new_process().start()  # 로컬 now < 캐시 issued_at → 잠시 차단, POST 없음
    _blocked("clock_untrusted")
    assert env.server.posts == 1
    env.jobs.run_all()
    assert env.server.posts == 1
    env.t.advance(300)
    _blocked("clock_untrusted")
    env.jobs.run_all()
    CrawlerRegistry.require()
    assert env.server.posts == 1


def test_clock_rollback_still_detected_with_server_ahead(env):
    env.server.now = lambda: env.t.w + 30
    env.first_key()
    env.t.advance(H)
    CrawlerRegistry.require()
    env.t.advance(-120)
    _blocked("clock_untrusted")


# ── Codex 전체 검수 1차: 재시작 + 시계 되돌림으로 잔량을 되살리지 못한다 ──

def test_restart_with_clock_rollback_cannot_restore_lease(env):
    env.first_key()
    env.t.advance(10)
    CrawlerRegistry.require()
    env.t.advance(300)
    CrawlerRegistry.require()  # 진입 때마다 관측 최고 시각을 저장한다
    env.new_process()
    env.t.advance(-300)  # 마지막 저장 시각 이전으로 되돌리고 재시작
    env.server.version_status = (503, {"error": "no_release"}, None)  # 오프라인
    env.gate.start()
    _blocked()
    assert env.gate.status()["state"] != "ready"


def test_rollback_before_next_entry_detected_by_monotonic(env):
    # Codex 2차: 사용 뒤 다음 진입 전에 wall 을 마지막 저장 시각으로 되돌려도 단조 경과로 잡는다
    env.first_key()
    env.t.advance(10)
    CrawlerRegistry.require()
    env.t.advance(0, mono=300)  # 300초가 흘렀지만 wall 은 되돌려 그대로
    _blocked("clock_untrusted")
    env.new_process()
    env.server.version_status = (503, {"error": "no_release"}, None)
    env.gate.start()
    _blocked()
    assert env.gate.status()["state"] != "ready"


def test_idle_time_with_wall_advancing_is_fine(env):
    env.first_key()
    for _ in range(3):
        env.t.advance(3600)
        CrawlerRegistry.require()


# ── 결정 A(Codex 3차): float 누적·초기화 시점 기준점·단조 허용 ──

def test_subsecond_repeated_entries_accumulate(env):
    env.first_key()
    env.t.advance(10)
    CrawlerRegistry.require()
    blocked = False
    for _ in range(600):
        env.t.advance(0, mono=0.5)  # wall 고정, 단조만 0.5초씩
        try:
            CrawlerRegistry.require()
        except CrawlerBlocked as e:
            assert e.reason == "clock_untrusted"
            blocked = True
            break
    assert blocked
    env.new_process()
    env.server.version_status = (503, {"error": "no_release"}, None)
    env.gate.start()
    assert env.gate.status()["state"] != "ready"


def test_rollback_right_after_publish_then_offline_restart(env):
    env.first_key()  # publish 가 기준점을 지금으로 잡는다
    env.t.advance(0, mono=300)  # 300초 사용 뒤 wall 을 발급 시각으로 되돌린 채 첫 진입
    _blocked("clock_untrusted")
    env.new_process()
    env.server.version_status = (503, {"error": "no_release"}, None)
    env.gate.start()
    assert env.gate.status()["state"] != "ready"


def test_monotonic_slightly_faster_than_wall_no_false_block(env):
    env.first_key()
    env.server.version_status = (503, {"error": "no_release"}, None)  # 오프라인
    for _ in range(72):
        env.t.advance(3600, mono=3600.18)  # 단조가 50ppm 빠름
        CrawlerRegistry.require()
        env.jobs.run_all()


def test_expiry_block_still_persists_monotonic_floor(env):
    # Codex 4차: 단조만 144시간+1초 흐르고 wall 을 되돌린 채 진입 → offline_deadline 으로 막혀도 하한을 저장
    env.first_key()
    env.server.version_status = (503, {"error": "no_release"}, None)
    env.t.advance(0, mono=144 * H + 1)
    _blocked("offline_deadline")
    assert env.state.get(FA).time_high_water >= T0 + 144 * H - 600
    env.new_process()
    env.gate.start()
    assert env.gate.status()["state"] != "ready"


# ── G7 실기: 복구 뒤 last_error 가 남지 않는다 ──

def test_first_key_clears_no_key_error(env):
    env.gate.start()
    assert env.gate.status()["last_error"] == "no_key"
    env.first_key()
    s = env.gate.status()
    assert s["state"] == "ready" and s["last_error"] is None


def test_corrupt_cache_recovery_clears_error(env):
    env.first_key()
    cache = env.slots.cache_dir(FA) / "bundle-cache.json"
    data = cache.read_bytes()
    cache.write_bytes(data[:-100])
    env.t.advance(60)
    env.new_process().start()
    s = env.gate.status()
    assert s["state"] == "ready" and env.server.posts == 2
    assert s["last_error"] is None


def test_version_recovery_clears_error(env):
    env.first_key()
    env.server.version_status = (503, {"error": "no_release"}, None)
    env.t.advance(72 * H)
    CrawlerRegistry.require()
    env.jobs.run_all()
    assert env.gate.status()["last_error"] is not None
    env.server.version_status = None
    env.t.advance(2 * H)
    CrawlerRegistry.require()
    env.jobs.run_all()
    s = env.gate.status()
    assert s["version_check_failed"] is False and s["last_error"] is None


def test_reverify_download_failure_keeps_error(env):
    env.first_key()
    env.server.publish()
    env.server.post_status = (503, {"error": "busy"}, None)
    with pytest.raises(V2Error) as e:
        env.gate.reverify()
    assert env.gate.status()["last_error"] == e.value.reason
