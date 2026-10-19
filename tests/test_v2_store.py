"""도매 v2 로컬 저장소(store) 시험."""
import json
import sys
from dataclasses import fields

import pytest

from domae_mcp.v2 import store as store_mod
from domae_mcp.v2.store import (MONOTONIC, BundleCache, InstallStore, KeyState, StateStore, StoreError, app_home,
                                data_dir)
from domae_mcp.v2.winprotect import DevPlainProtector, purpose

FP_A = bytes(range(32))
FP_B = bytes(range(1, 33))
T0 = 1_800_000_000


def _protectors():
    out = [pytest.param(DevPlainProtector, id="devplain")]
    if sys.platform == "win32":
        from domae_mcp.v2.winprotect import DpapiProtector
        out.append(pytest.param(DpapiProtector, id="dpapi"))
    return out


@pytest.fixture(params=_protectors())
def prot(request):
    return request.param()


def _bad_files(root, name):
    return [p for p in root.iterdir() if p.name.startswith(name + ".bad-")]


def test_nonce_created_once_and_stable(tmp_path, prot):
    n1 = InstallStore(tmp_path, prot).nonce()
    assert len(n1) == 32
    assert InstallStore(tmp_path, prot).nonce() == n1
    assert n1 not in (tmp_path / "install.dpapi").read_bytes() or not prot.protected


def test_nonce_unreadable_quarantined(tmp_path, prot):
    (tmp_path / "install.dpapi").write_bytes(b"garbage")
    with pytest.raises(StoreError) as e:
        InstallStore(tmp_path, prot).nonce()
    assert e.value.reason == "install_unreadable"
    assert _bad_files(tmp_path, "install.dpapi") and not (tmp_path / "install.dpapi").exists()


def test_nonce_wrong_length_quarantined(tmp_path, prot):
    (tmp_path / "install.dpapi").write_bytes(prot.protect(b"x" * 31, purpose("install")))
    with pytest.raises(StoreError) as e:
        InstallStore(tmp_path, prot).nonce()
    assert e.value.reason == "install_unreadable"
    assert _bad_files(tmp_path, "install.dpapi")


def test_data_dir_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("MAIPHARM_DOMAE_HOME", str(tmp_path))
    assert app_home() == tmp_path
    assert data_dir() == tmp_path / "v2" and (tmp_path / "v2").is_dir()


def test_app_home_shared_by_config(tmp_path, monkeypatch):
    from domae_mcp.local.config import ConfigManager
    monkeypatch.setenv("MAIPHARM_DOMAE_HOME", str(tmp_path / "home"))
    assert ConfigManager().base_dir == data_dir().parent


def test_state_monotonic_fields_reject_decrease(tmp_path, prot):
    st = StateStore(tmp_path, prot)

    def high(s):
        s.highest_release_seq, s.min_release_seq, s.min_protocol_version = 5, 5, 2
        s.device_generation, s.time_high_water = 3, T0
        return s

    st.update(FP_A, high)
    path = tmp_path / "state.dpapi"
    before = path.read_bytes()
    decreases = {"highest_release_seq": 4, "min_release_seq": 4, "min_protocol_version": 1,
                 "device_generation": 2, "time_high_water": T0 - 1}
    assert set(decreases) == set(MONOTONIC)
    for name, value in list(decreases.items()) + [("device_generation", None)]:
        with pytest.raises(StoreError) as e:
            st.update(FP_A, lambda s, n=name, v=value: (setattr(s, n, v), s)[1])
        assert e.value.reason == "monotonic"
        assert path.read_bytes() == before
    assert StateStore(tmp_path, prot).get(FP_A).highest_release_seq == 5

    def free(s):
        s.backoff_until, s.backoff_level, s.revoked = T0 + 5, 3, {"scope": "key", "at": T0}
        return s

    st.update(FP_A, free)

    def free_back(s):
        s.backoff_until, s.backoff_level, s.revoked = 0, 0, None
        return s

    got = st.update(FP_A, free_back)
    assert (got.backoff_until, got.backoff_level, got.revoked) == (0, 0, None)


def test_device_generation_none_to_value_allowed(tmp_path, prot):
    st = StateStore(tmp_path, prot)
    assert st.get(FP_A).device_generation is None
    assert st.update(FP_A, lambda s: (setattr(s, "device_generation", 0), s)[1]).device_generation == 0


def test_state_forget_others_and_forget(tmp_path, prot):
    st = StateStore(tmp_path, prot)
    st.update(FP_A, lambda s: (setattr(s, "highest_release_seq", 1), s)[1])
    st.update(FP_B, lambda s: (setattr(s, "highest_release_seq", 2), s)[1])
    st.forget_others(FP_B)
    st2 = StateStore(tmp_path, prot)
    assert not st2.has(FP_A) and st2.get(FP_B).highest_release_seq == 2
    st2.forget(FP_B)
    assert not StateStore(tmp_path, prot).has(FP_B)


def test_update_create_false_refuses_forgotten_key(tmp_path, prot):
    st = StateStore(tmp_path, prot)
    st.update(FP_A, lambda s: s)
    st.forget(FP_A)
    with pytest.raises(StoreError) as e:
        st.update(FP_A, lambda s: s, create=False)
    assert e.value.reason == "forgotten"
    assert not st.has(FP_A)
    with pytest.raises(StoreError):
        st.reset_high_water(FP_A, T0, T0)


def test_reset_high_water_lowers_and_persists(tmp_path, prot):
    st = StateStore(tmp_path, prot)
    st.update(FP_A, lambda s: (setattr(s, "time_high_water", T0 + 3600), s)[1])
    with pytest.raises(StoreError) as e:
        st.reset_high_water(FP_B, T0, T0)
    assert e.value.reason == "forgotten"
    assert st.reset_high_water(FP_A, T0, T0 + 10).time_high_water == T0 + 10
    assert StateStore(tmp_path, prot).get(FP_A).time_high_water == T0 + 10
    st.update(FP_A, lambda s: (setattr(s, "time_high_water", T0 + 3600), s)[1])
    with pytest.raises(StoreError) as e:
        st.update(FP_A, lambda s: (setattr(s, "time_high_water", T0 + 10), s)[1])
    assert e.value.reason == "monotonic"
    with pytest.raises(StoreError) as e:
        st.reset_high_water(FP_A, T0, T0 + 601)
    assert e.value.reason == "not_fresh"
    with pytest.raises(StoreError):
        st.reset_high_water(FP_A, T0 + 601, T0)


def test_state_unreadable_starts_empty_once(tmp_path, prot):
    (tmp_path / "state.dpapi").write_bytes(b"not a blob")
    st = StateStore(tmp_path, prot)
    with pytest.raises(StoreError) as e:
        st.get(FP_A)
    assert e.value.reason == "state_unreadable"
    assert _bad_files(tmp_path, "state.dpapi")
    assert st.get(FP_A) == KeyState()
    st.update(FP_A, lambda s: s)
    assert StateStore(tmp_path, prot).has(FP_A)


@pytest.mark.parametrize("field,value", [("highest_release_seq", 1.0), ("backoff_level", True),
                                         ("time_high_water", "1"), ("min_release_seq", 0)])
def test_state_rejects_non_int_fields(tmp_path, prot, field, value):
    entry = {f.name: getattr(KeyState(), f.name) for f in fields(KeyState)}
    entry[field] = value
    body = json.dumps({"format": 1, "keys": {FP_A.hex(): entry}}).encode()
    (tmp_path / "state.dpapi").write_bytes(prot.protect(body, purpose("state")))
    with pytest.raises(StoreError) as e:
        StateStore(tmp_path, prot).get(FP_A)
    assert e.value.reason == "state_unreadable"


def test_state_other_purpose_unreadable(tmp_path, prot):
    body = json.dumps({"format": 1, "keys": {}}).encode()
    (tmp_path / "state.dpapi").write_bytes(prot.protect(body, purpose("install")))
    with pytest.raises(StoreError):
        StateStore(tmp_path, prot).get(FP_A)


def test_update_rejects_invalid_value(tmp_path, prot):
    st = StateStore(tmp_path, prot)
    with pytest.raises(StoreError) as e:
        st.update(FP_A, lambda s: (setattr(s, "backoff_until", 1.5), s)[1])
    assert e.value.reason == "invalid"
    assert not (tmp_path / "state.dpapi").exists()


def test_bundle_cache_roundtrip_and_corrupt(tmp_path):
    c = BundleCache(tmp_path)
    assert c.read() is None
    env = {"payload": "{}", "signing_key_id": "k", "signature": "s"}
    c.write(env, b"\x01" * 40)
    got = c.read()
    assert got == {"envelope": env, "wrapped_session_key": b"\x01" * 40}
    c.path.write_text("{not json", encoding="utf-8")
    assert c.read() is None
    assert _bad_files(tmp_path, "bundle-cache.json") and not c.path.exists()
    c.write(env, b"x")
    raw = json.loads(c.path.read_text(encoding="utf-8"))
    raw["envelope"]["extra"] = "x"
    c.path.write_text(json.dumps(raw), encoding="utf-8")
    assert c.read() is None
    c.write(env, b"x")
    c.delete()
    c.delete()
    assert c.read() is None


def test_store_module_has_no_print_of_secrets():
    src = open(store_mod.__file__, encoding="utf-8").read()
    assert "print(" not in src and "logger" not in src


def test_bad_key_fp_rejected_without_write(tmp_path, prot):
    st = StateStore(tmp_path, prot)
    with pytest.raises(StoreError) as e:
        st.update(b"short", lambda s: s)
    assert e.value.reason == "invalid"
    assert not (tmp_path / "state.dpapi").exists()


def test_returned_state_is_a_copy(tmp_path, prot):
    st = StateStore(tmp_path, prot)
    st.update(FP_A, lambda s: (setattr(s, "revoked", {"scope": "key", "at": T0}), s)[1])
    got = st.get(FP_A)
    got.revoked["scope"] = "device"
    assert st.get(FP_A).revoked == {"scope": "key", "at": T0}
