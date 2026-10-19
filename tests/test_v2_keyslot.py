"""도매 v2 키 슬롯 시험 (계획 Task 6, P13). 커밋은 활성 포인터 교체 1회뿐이다."""
import pytest

from domae_mcp.v2 import keyslot as keyslot_mod
from domae_mcp.v2.client import V2Client
from domae_mcp.v2.encoding import key_fp_from_bearer
from domae_mcp.v2.keyslot import KeySlots
from domae_mcp.v2.loader import V2Loader
from domae_mcp.v2.runtime import local_runtime_id
from domae_mcp.v2.store import BundleCache, StateStore
from v2_fakes import (DEVICE_HEX, TEST_BEARER, TEST_BEARER_2, TEST_TRUST, FakeTime, FakeV2Server, fake_artifacts,
                      pick_protector)

FA, FB = key_fp_from_bearer(TEST_BEARER), key_fp_from_bearer(TEST_BEARER_2)


@pytest.fixture
def prot():
    return pick_protector()


@pytest.fixture
def slots(tmp_path, prot):
    return KeySlots(tmp_path / "v2", prot)


def _loader(tmp_path, slots, prot, bearer, server, t):
    http = server.client()
    return V2Loader(bearer, slots.cache_dir(key_fp_from_bearer(bearer)), StateStore(tmp_path / "v2", prot), prot,
                    client=V2Client(bearer, DEVICE_HEX, http=http, base="https://pharmsq.com/api/domae"),
                    clock=t.clock(), trust=TEST_TRUST, device_id_hex=DEVICE_HEX, runtime_id=local_runtime_id())


def _activate(slots, bearer):
    slots.stage(bearer)
    slots.commit(key_fp_from_bearer(bearer))


def test_no_key_initially(slots):
    assert slots.active() is None


def test_commit_is_single_pointer_replace(slots, monkeypatch):
    _activate(slots, TEST_BEARER)
    slots.stage(TEST_BEARER_2)
    assert slots.active() == (FA, TEST_BEARER)
    writes = []
    real = keyslot_mod.atomic_write_bytes
    monkeypatch.setattr(keyslot_mod, "atomic_write_bytes", lambda p, d: (writes.append(p.name), real(p, d))[1])
    slots.commit(FB)
    assert writes == ["active.dpapi"]
    assert slots.active() == (FB, TEST_BEARER_2)
    assert slots.cache_dir(FA).exists()  # 정리 전에는 남아 있다


def test_stage_failure_keeps_active(slots, monkeypatch):
    _activate(slots, TEST_BEARER)

    def boom(p, d):
        raise OSError("disk")

    monkeypatch.setattr(keyslot_mod, "atomic_write_bytes", boom)
    with pytest.raises(OSError):
        slots.stage(TEST_BEARER_2)
    monkeypatch.undo()
    assert slots.active() == (FA, TEST_BEARER)


def test_cache_write_failure_before_commit_keeps_active(tmp_path, slots, prot, monkeypatch):
    _activate(slots, TEST_BEARER)
    slots.stage(TEST_BEARER_2)
    t = FakeTime()
    ld = _loader(tmp_path, slots, prot, TEST_BEARER_2, FakeV2Server(fake_artifacts(), now=lambda: t.w), t)
    p = ld.prepare_download()

    def boom(self, env, wrapped):
        raise OSError("disk")

    monkeypatch.setattr(BundleCache, "write", boom)
    with pytest.raises(OSError):
        ld.write_cache(p)
    assert slots.active() == (FA, TEST_BEARER)


def test_commit_failure_keeps_active_and_cleanup_removes_candidate(slots, monkeypatch):
    _activate(slots, TEST_BEARER)
    slots.stage(TEST_BEARER_2)

    def boom(p, d):
        raise OSError("disk")

    monkeypatch.setattr(keyslot_mod, "atomic_write_bytes", boom)
    with pytest.raises(OSError):
        slots.commit(FB)
    monkeypatch.undo()
    assert slots.active() == (FA, TEST_BEARER)
    slots.cleanup()
    assert not slots.cache_dir(FB).exists() and slots.cache_dir(FA).exists()


def test_commit_refuses_unstaged_key(slots):
    _activate(slots, TEST_BEARER)
    with pytest.raises(FileNotFoundError):
        slots.commit(FB)
    assert slots.active() == (FA, TEST_BEARER)


def test_crash_after_commit_restarts_on_new_key(tmp_path, slots, prot):
    _activate(slots, TEST_BEARER)
    slots.stage(TEST_BEARER_2)
    t = FakeTime()
    server = FakeV2Server(fake_artifacts(), now=lambda: t.w)
    ld = _loader(tmp_path, slots, prot, TEST_BEARER_2, server, t)
    assert ld.write_cache(ld.prepare_download())
    slots.commit(FB)
    # 커밋 직후 중단 → 새 프로세스
    again = KeySlots(tmp_path / "v2", prot)
    assert again.active() == (FB, TEST_BEARER_2)
    t.advance(60)
    cached = _loader(tmp_path, again, prot, TEST_BEARER_2, server, t).prepare_cached()
    assert cached.mode == "cache" and server.posts == 1


def test_cleanup_failure_is_retried_next_start(slots, monkeypatch):
    _activate(slots, TEST_BEARER)
    slots.stage(TEST_BEARER_2)

    def boom(path):
        raise OSError("busy")

    monkeypatch.setattr(keyslot_mod.shutil, "rmtree", boom)
    slots.cleanup()  # 예외 없이 경고만
    assert slots.cache_dir(FB).exists()
    monkeypatch.undo()
    slots.cleanup()
    assert not slots.cache_dir(FB).exists() and slots.active() == (FA, TEST_BEARER)


def test_pointer_corrupt_means_no_key(slots):
    _activate(slots, TEST_BEARER)
    ptr = slots.cache_dir(FA).parent.parent / "active.dpapi"
    ptr.write_bytes(b"junk")
    assert slots.active() is None
    assert not ptr.exists()
    assert [p for p in ptr.parent.iterdir() if p.name.startswith("active.dpapi.bad-")]


def test_key_file_bound_to_its_fp(slots):
    _activate(slots, TEST_BEARER)
    _activate(slots, TEST_BEARER_2)
    # B 의 키 파일을 A 폴더로 옮겨 놓아도 A 로 풀리지 않는다
    a_file = slots.cache_dir(FA) / "api_key.dpapi"
    a_file.write_bytes((slots.cache_dir(FB) / "api_key.dpapi").read_bytes())
    assert slots.read_bearer(FA) is None


def test_cleanup_without_pointer_removes_candidates(slots):
    slots.stage(TEST_BEARER)
    slots.cleanup()
    assert not slots.cache_dir(FA).exists()


def test_pointer_read_oserror_not_quarantined(slots, monkeypatch):
    _activate(slots, TEST_BEARER)
    slots.stage(TEST_BEARER_2)
    ptr = slots.cache_dir(FA).parent.parent / "active.dpapi"
    real = type(ptr).read_bytes

    def locked(self):
        if self.name == "active.dpapi":
            raise PermissionError("locked")
        return real(self)

    monkeypatch.setattr(type(ptr), "read_bytes", locked)
    assert slots.active() is None
    slots.cleanup()
    monkeypatch.undo()
    assert ptr.exists() and slots.cache_dir(FB).exists()
    assert slots.active() == (FA, TEST_BEARER)


def test_commit_refuses_unreadable_key_file(slots):
    _activate(slots, TEST_BEARER)
    slots.stage(TEST_BEARER_2)
    (slots.cache_dir(FB) / "api_key.dpapi").write_bytes(b"junk")
    with pytest.raises(FileNotFoundError):
        slots.commit(FB)
    assert slots.active() == (FA, TEST_BEARER)
