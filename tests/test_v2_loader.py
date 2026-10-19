"""도매 v2 로더(network·cache 모드) 시험 (계획 Task 6). 가짜 서버(MockTransport)·시험 서명 키만 쓴다."""
import base64
import builtins
import json
import marshal
import os

import httpx
import pytest

from domae_mcp.v2 import loader as loader_mod
from domae_mcp.v2.artifacts import MAX_ARTIFACT_BYTES
from domae_mcp.v2.client import V2Client
from domae_mcp.v2.encoding import key_fp_from_bearer
from domae_mcp.v2.envelope import DOMAIN_RELEASE
from domae_mcp.v2.keyslot import KeySlots
from domae_mcp.v2.loader import V2Error, V2Loader, V2Revoked
from domae_mcp.v2.runtime import local_runtime_id
from domae_mcp.v2.store import BundleCache, StateStore
from v2_fakes import (DEVICE_HEX, OTHER_KEY_ID, TEST_BEARER, TEST_TRUST, CountingProtector, FakeTime, FakeV2Server, H,
                      T0, b64, fake_artifacts, make_revocation_envelope, pick_protector, sign)

FP = key_fp_from_bearer(TEST_BEARER)


class Env:
    def __init__(self, root):
        self.root = root / "v2"
        self.t = FakeTime(T0)
        self.prot = CountingProtector(pick_protector())
        self.state = StateStore(self.root, self.prot)
        self.slots = KeySlots(self.root, self.prot)
        self.plain = fake_artifacts()
        self.server = FakeV2Server(self.plain, now=lambda: self.t.w)
        self.current = True

    def loader(self, bearer=TEST_BEARER, state=None, **kw):
        http = self.server.client()
        client = V2Client(bearer, DEVICE_HEX, http=http, base="https://pharmsq.com/api/domae")
        fp = key_fp_from_bearer(bearer)
        return V2Loader(bearer, self.slots.cache_dir(fp), state or self.state, self.prot, client=client,
                        clock=self.t.clock(), trust=TEST_TRUST, still_current=kw.pop("still_current", lambda: self.current),
                        device_id_hex=DEVICE_HEX, runtime_id=local_runtime_id(), **kw)

    def cache(self, bearer=TEST_BEARER):
        return BundleCache(self.slots.cache_dir(key_fp_from_bearer(bearer)))

    def install(self):
        ld = self.loader()
        v = ld.check_version()
        assert ld.needs_download(v, None)
        p = ld.prepare_download()
        assert ld.write_cache(p)
        return p


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


def _reason(fn, *a):
    with pytest.raises(V2Error) as e:
        fn(*a)
    return e.value


def _counter(monkeypatch, obj, name):
    calls = {"n": 0}
    real = getattr(obj, name)

    def wrapped(*a, **k):
        calls["n"] += 1
        return real(*a, **k)

    monkeypatch.setattr(obj, name, wrapped)
    return calls


# ── 정상 ──

def test_first_install_prepares_and_caches(env):
    p = env.install()
    assert p.mode == "network" and p.plain == env.plain and p.release_seq == 1
    assert p.expires_at == T0 + 72 * H and p.offline_deadline == T0 + 144 * H
    assert env.cache().read() is not None
    ks = env.state.get(FP)
    assert (ks.highest_release_seq, ks.device_generation, ks.time_high_water) == (1, 0, T0)
    assert env.server.posts == 1


def test_restart_uses_cache_without_post(env):
    env.install()
    env.t.advance(600)
    ld = env.loader(state=StateStore(env.root, env.prot))
    cached = ld.prepare_cached()
    assert cached.mode == "cache" and cached.plain == env.plain
    v = ld.check_version()
    assert ld.needs_download(v, cached) is False
    assert env.server.posts == 1


def test_new_release_triggers_one_post(env):
    env.install()
    env.server.publish()
    ld = env.loader()
    cached = ld.prepare_cached()
    v = ld.check_version()
    assert ld.needs_download(v, cached)
    p = ld.prepare_download()
    assert p.release_seq == 2 and env.server.posts == 2


# ── 순서 ──

def test_marshal_not_called_in_loader(env, monkeypatch):
    loads = _counter(monkeypatch, marshal, "loads")
    execs = _counter(monkeypatch, builtins, "exec")
    env.install()
    env.loader().prepare_cached()
    assert loads["n"] == 0 and execs["n"] == 0


def test_signature_tamper_stops_before_decrypt(env, monkeypatch):
    dec = _counter(monkeypatch, loader_mod, "decrypt_modules")

    def tamper(e):
        sig = bytearray(base64.b64decode(e["signature"]))
        sig[0] ^= 1
        return {**e, "signature": b64(bytes(sig))}

    env.server.post_envelope = tamper
    e = _reason(env.loader().prepare_download)
    assert e.reason == "envelope" and dec["n"] == 0


def test_cache_order_dpapi_after_context(env, tmp_path):
    env.install()
    other = Env(tmp_path / "other")
    other.server.plain = env.plain
    # A 의 캐시를 B(다른 key_fp) 폴더에 둔다
    src = env.cache().read()
    other.cache("dmk_test_" + "z" * 32).write(src["envelope"], src["wrapped_session_key"])
    before = other.prot.unprotects
    e = _reason(other.loader(bearer="dmk_test_" + "z" * 32).prepare_cached)
    assert e.reason == "payload" and e.detail == "identity"
    assert other.prot.unprotects == before


def test_untrusted_key_id_rejected(env):
    env.server.post_sign_kw = {"key_id": OTHER_KEY_ID}
    e = _reason(env.loader().prepare_download)
    assert (e.reason, e.detail) == ("envelope", "unknown_signing_key")
    _assert_nothing_saved_but_backoff(env)


def test_wrong_domain_signature_rejected(env):
    env.server.post_envelope = lambda e: sign(DOMAIN_RELEASE, e["payload"])
    e = _reason(env.loader().prepare_download)
    assert (e.reason, e.detail) == ("envelope", "bad_signature")
    _assert_nothing_saved_but_backoff(env)


# ── 크기 ──

def test_artifact_over_4mib_rejected_before_decrypt(env, monkeypatch):
    dec = _counter(monkeypatch, loader_mod, "decrypt_modules")
    loads = _counter(monkeypatch, marshal, "loads")
    env.server.post_mutate = lambda p: p["manifest"]["artifacts"]["fake_alpha"].update(size=MAX_ARTIFACT_BYTES + 1)
    e = _reason(env.loader().prepare_download)
    assert (e.reason, e.detail) == ("load", "too_large")
    assert dec["n"] == 0 and loads["n"] == 0


# ── 바인딩·재생 ──

def _assert_nothing_saved_but_backoff(env):
    ks = env.state.get(FP)
    assert (ks.highest_release_seq, ks.device_generation, ks.min_release_seq) == (0, None, 1)
    assert ks.backoff_level == 1 and ks.backoff_until > env.t.w
    assert env.cache().read() is None


def test_other_request_response_rejected(env):
    env.server.post_mutate = lambda p: p.update(request_id=b64(os.urandom(16)))
    e = _reason(env.loader().prepare_download)
    assert (e.reason, e.detail) == ("payload", "request_mismatch")
    _assert_nothing_saved_but_backoff(env)


def test_old_release_seq_rejected(env):
    env.state.update(FP, lambda s: (setattr(s, "highest_release_seq", 5), s)[1])
    e = _reason(env.loader().prepare_download)
    assert (e.reason, e.detail) == ("payload", "downgrade")


def test_same_seq_other_hash_no_post(env):
    env.install()
    env.server.bundle_hash = "cd" * 32
    ld = env.loader()
    cached = ld.prepare_cached()
    v = ld.check_version()
    assert _reason(ld.needs_download, v, cached).reason == "bundle_hash_conflict"
    assert env.server.posts == 1


def test_min_release_seq_above_cache_proceeds_to_download(env):
    env.install()
    env.server.publish()
    env.server.min_release_seq = 2
    ld = env.loader()
    cached = ld.prepare_cached()
    v = ld.check_version()
    assert v.min_release_seq == 2
    assert ld.needs_download(v, cached)
    assert ld.prepare_download().release_seq == 2
    assert env.server.posts == 2
    # 저장된 정책으로는 옛 캐시를 더 쓰지 않는다
    e = _reason(env.loader().prepare_cached)
    assert "downgrade" in (e.reason, e.detail)


def test_min_protocol_above_client_stops_with_update_notice(env):
    env.server.min_protocol_version = 3
    ld = env.loader()
    assert _reason(ld.check_version).reason == "protocol_too_old"
    assert env.state.get(FP).min_protocol_version == 3
    assert env.server.posts == 0


# ── 시간 ──

def test_cache_prepare_144h_refused(env):
    env.install()
    env.t.advance(144 * H)
    e = _reason(env.loader().prepare_cached)
    assert (e.reason, e.detail) == ("payload", "offline_deadline")


def test_cache_prepare_after_clock_regression_refused(env):
    env.install()
    env.t.advance(-1)
    assert _reason(env.loader().prepare_cached).reason == "clock"


def test_version_success_resets_high_water_after_rollback(env):
    env.install()
    env.state.update(FP, lambda s: (setattr(s, "time_high_water", T0 + H), s)[1])
    env.t.advance(10)
    ld = env.loader()
    assert _reason(ld.prepare_cached).reason == "clock"
    ld.check_version()
    assert env.state.get(FP).time_high_water == T0 + 10
    assert ld.prepare_cached().release_seq == 1


# ── 철회 ──

def test_restart_with_cache_version_revoked_not_published(env):
    env.install()
    ld = env.loader()
    assert ld.prepare_cached().mode == "cache"
    env.server.mode = "revoke_key"
    with pytest.raises(V2Revoked) as e:
        ld.check_version()
    assert e.value.reason == "key_revoked"
    assert env.cache().read() is None
    assert env.state.get(FP).revoked["scope"] == "key"
    with pytest.raises(V2Revoked):
        env.loader().prepare_cached()


def test_signed_key_revocation_wipes_cache(env):
    env.install()
    env.server.mode = "revoke_key"
    with pytest.raises(V2Revoked):
        env.loader().prepare_download()
    assert env.cache().read() is None and env.state.get(FP).revoked is not None


def test_signed_device_revocation_old_generation_ignored(env):
    env.install()
    env.state.update(FP, lambda s: (setattr(s, "device_generation", 2), s)[1])
    env.server.device_generation = 1
    env.server.mode = "revoke_device"
    e = _reason(env.loader().check_version)
    assert not isinstance(e, V2Revoked) and e.reason == "auth_error"
    assert env.state.get(FP).revoked is None and env.cache().read() is not None


def test_unsigned_403_is_auth_error_not_revocation(env):
    env.install()
    env.server.version_status = (403, {"error": "forbidden"}, None)
    e = _reason(env.loader().check_version)
    assert not isinstance(e, V2Revoked) and e.reason == "auth_error"
    assert env.state.get(FP).revoked is None


def test_replayed_revocation_other_request_ignored(env):
    env.install()
    replay = make_revocation_envelope("key", b64(os.urandom(16)), FP, DEVICE_HEX, T0)
    env.server.version_status = (403, replay, None)
    e = _reason(env.loader().check_version)
    assert not isinstance(e, V2Revoked) and e.reason == "auth_error"
    assert env.state.get(FP).revoked is None and env.cache().read() is not None


def test_revoked_cleared_only_after_full_success(env):
    env.install()
    env.server.mode = "revoke_key"
    with pytest.raises(V2Revoked):
        env.loader().check_version()
    env.server.mode = "normal"
    # 버전 확인 없이 번들만 받으면 지우지 않는다
    env.loader().prepare_download()
    assert env.state.get(FP).revoked is not None
    # 버전 확인 성공 뒤 번들 검증이 실패해도 지우지 않는다
    env.t.advance(H)
    ld = env.loader()
    ld.check_version()
    env.server.post_mutate = lambda p: p["modules"]["fake_alpha"].update(tag=b64(bytes(16)))
    with pytest.raises(V2Error):
        ld.prepare_download()
    assert env.state.get(FP).revoked is not None
    # 버전 확인과 번들 검증이 모두 성공해야 지운다
    env.server.post_mutate = None
    env.state.update(FP, lambda s: (setattr(s, "backoff_until", 0), s)[1])
    ld = env.loader()
    ld.check_version()
    ld.prepare_download()
    assert env.state.get(FP).revoked is None


# ── 오류 ──

def _assert_backoff_blocks_post(env):
    posts = env.server.posts
    ks = StateStore(env.root, env.prot).get(FP)
    assert ks.backoff_until > env.t.w and ks.backoff_level == 1
    assert _reason(env.loader(state=StateStore(env.root, env.prot)).prepare_download).reason == "backoff"
    assert env.server.posts == posts


def test_post_200_but_gcm_fail_sets_backoff(env):
    env.server.post_mutate = lambda p: p["modules"]["fake_alpha"].update(tag=b64(bytes(16)))
    e = _reason(env.loader().prepare_download)
    assert (e.reason, e.detail) == ("crypto", "gcm")
    _assert_backoff_blocks_post(env)


def test_post_200_but_manifest_fail_sets_backoff(env):
    env.server.post_mutate = lambda p: p["manifest"].update(base_abi=2)
    e = _reason(env.loader().prepare_download)
    assert (e.reason, e.detail) == ("load", "abi")
    _assert_backoff_blocks_post(env)


def test_device_limit_no_state_saved(env):
    env.server.post_status = (403, {"error": "device_limit"}, None)
    assert _reason(env.loader().prepare_download).reason == "device_limit"
    ks = env.state.get(FP)
    assert ks.revoked is None and ks.device_generation is None and ks.highest_release_seq == 0


def test_429_sets_persistent_backoff(env):
    env.server.post_status = (429, {"error": "daily_limit"}, {"Retry-After": "7200"})
    e = _reason(env.loader().prepare_download)
    assert (e.reason, e.retry_after) == ("rate_limited", 7200)
    assert StateStore(env.root, env.prot).get(FP).backoff_until >= T0 + 7200
    _assert_backoff_blocks_post(env)


def test_503_keeps_cache_and_marks_version_failed(env):
    env.install()
    env.server.version_status = (503, {"error": "no_release"}, None)
    ld = env.loader()
    assert ld.prepare_cached().mode == "cache"
    e = _reason(ld.check_version)
    assert (e.reason, e.detail) == ("transient", "no_release")
    assert env.server.posts == 1


def test_dpapi_failure_quarantines_cache(env):
    env.install()
    c = env.cache()
    src = c.read()
    c.write(src["envelope"], b"garbage-wrapped-key")
    assert _reason(env.loader().prepare_cached).reason == "store"
    assert not c.path.exists()
    assert [p for p in c.path.parent.iterdir() if p.name.startswith("bundle-cache.json.bad-")]


# ── 세대 ──

def test_same_release_generation_raise_needs_download(env):
    env.install()
    env.server.device_generation = 1
    ld = env.loader()
    cached = ld.prepare_cached()
    v = ld.check_version()
    assert v.release_seq == cached.release_seq and v.device_generation == 1
    assert ld.needs_download(v, cached)
    assert env.state.get(FP).device_generation == 1


def test_version_generation_raised_then_post_fails_old_cache_rejected(env):
    env.install()
    env.server.device_generation = 1
    ld = env.loader()
    ld.check_version()
    env.server.post_status = (503, {"error": "bundle_unavailable"}, None)
    assert _reason(ld.prepare_download).reason == "transient"
    e = _reason(env.loader().prepare_cached)
    assert (e.reason, e.detail) == ("payload", "old_generation")


# ── 경합 ──

def test_stale_prepare_after_key_switch_discarded(env):
    env.current = False
    assert _reason(env.loader().prepare_download).reason == "stale"
    assert not env.state.has(FP)
    assert env.cache().read() is None


def test_stale_revocation_after_key_switch_ignored(env):
    env.install()
    env.current = False
    env.server.mode = "revoke_key"
    assert _reason(env.loader().check_version).reason == "stale"
    assert env.state.get(FP).revoked is None and env.cache().read() is not None


def test_stale_write_cache_skipped(env):
    ld = env.loader()
    p = ld.prepare_download()
    env.current = False
    assert ld.write_cache(p) is False and env.cache().read() is None


def test_session_key_wrapped_with_bound_purpose(env):
    env.install()
    old = env.cache().read()
    env.server.publish()
    ld = env.loader()
    ld.write_cache(ld.prepare_download())
    new = env.cache().read()
    # 새 캐시의 envelope 에 옛 캐시의 감싼 세션 키를 끼우면 DPAPI purpose 가 달라 풀리지 않는다
    env.cache().write(new["envelope"], old["wrapped_session_key"])
    assert _reason(env.loader().prepare_cached).reason == "store"


def test_network_min_release_above_bundle_stores_policy_and_rejects(env):
    env.server.post_mutate = lambda p: p["manifest"].update(min_release_seq=2)
    e = _reason(env.loader().prepare_download)
    assert e.reason == "downgrade"
    ks = env.state.get(FP)
    assert ks.min_release_seq == 2 and ks.highest_release_seq == 0 and ks.backoff_level == 1


def test_revocation_still_revokes_when_state_write_fails(env, monkeypatch):
    env.install()
    env.server.mode = "revoke_key"
    from domae_mcp.v2.store import StoreError

    def broken(*a, **k):
        raise StoreError("invalid")

    monkeypatch.setattr(env.state, "update", broken)
    with pytest.raises(V2Revoked):
        env.loader().check_version()
    assert env.cache().read() is None


def test_post_200_store_failure_sets_backoff(env, monkeypatch):
    from domae_mcp.v2.winprotect import ProtectError

    real = env.prot.protect

    def broken(data, purpose):
        if b"session-key" in purpose:
            raise ProtectError("protect")
        return real(data, purpose)

    monkeypatch.setattr(env.prot, "protect", broken)
    e = _reason(env.loader().prepare_download)
    monkeypatch.undo()
    assert e.reason == "store"
    assert env.state.get(FP).backoff_level == 1


def test_gcm_failure_keeps_lower_cache_usable(env):
    env.install()
    env.server.publish()
    env.server.post_mutate = lambda p: p["modules"]["fake_alpha"].update(tag=b64(bytes(16)))
    with pytest.raises(V2Error):
        env.loader().prepare_download()
    assert env.state.get(FP).highest_release_seq == 1
    env.t.advance(60)
    assert env.loader().prepare_cached().release_seq == 1


def test_prepared_repr_hides_secrets(env):
    p = env.install()
    r = repr(p)
    assert "plain" not in r and "wrapped" not in r and "key_fp" not in r


# ── 신뢰 목록 ──

def test_production_trust_is_single_fixed_key(monkeypatch):
    import sys
    import types
    from domae_mcp.v2 import trust
    assert list(trust.PRODUCTION_TRUST) == ["ed25519-0477f26ca36a1ebd"]
    assert len(trust.PRODUCTION_TRUST["ed25519-0477f26ca36a1ebd"]) == 32
    monkeypatch.delenv("DOMAE_V2_TRUST_FILE", raising=False)
    monkeypatch.delitem(sys.modules, "domae_mcp._build_info", raising=False)
    assert trust.active_trust() == trust.PRODUCTION_TRUST


def test_trust_file_only_outside_release(tmp_path, monkeypatch):
    import sys
    import types
    from domae_mcp.v2 import trust
    f = tmp_path / "trust.json"
    f.write_text(json.dumps({"signing_key_id": "ed25519-test-only", "public_key": b64(TEST_TRUST["ed25519-test-only"])}),
                 encoding="utf-8")
    monkeypatch.setenv("DOMAE_V2_TRUST_FILE", str(f))
    monkeypatch.delitem(sys.modules, "domae_mcp._build_info", raising=False)
    assert set(trust.active_trust()) == {"ed25519-0477f26ca36a1ebd", "ed25519-test-only"}
    fake = types.ModuleType("domae_mcp._build_info")
    fake.KIND = "release"
    monkeypatch.setitem(sys.modules, "domae_mcp._build_info", fake)
    assert trust.active_trust() == trust.PRODUCTION_TRUST
    fake.KIND = "test"
    f.write_text(json.dumps({"k": b64(b"x" * 31)}), encoding="utf-8")
    with pytest.raises(ValueError):
        trust.active_trust()


def test_revocation_raises_even_if_all_storage_fails(env, monkeypatch):
    env.install()
    env.server.mode = "revoke_key"
    from domae_mcp.v2 import loader as L
    from domae_mcp.v2.store import BundleCache, StoreError

    def broken(*a, **k):
        raise StoreError("invalid")

    def broken_os(*a, **k):
        raise PermissionError("locked")

    monkeypatch.setattr(env.state, "update", broken)
    monkeypatch.setattr(BundleCache, "delete", broken_os)
    monkeypatch.setattr(L, "quarantine", broken_os)
    with pytest.raises(V2Revoked):
        env.loader().check_version()
