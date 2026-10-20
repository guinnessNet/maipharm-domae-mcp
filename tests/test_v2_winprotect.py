"""도매 v2 로컬 보호 저장(winprotect) 시험. DPAPI 시험은 Windows 에서만 돈다."""
import os
import sys

import pytest

from domae_mcp.v2 import winprotect
from domae_mcp.v2.winprotect import (DevPlainProtector, ProtectError, atomic_write_bytes, default_protector,
                                     purpose, quarantine)

win_only = pytest.mark.skipif(sys.platform != "win32", reason="DPAPI")


@win_only
def test_dpapi_roundtrip_and_purpose_binding():
    p = winprotect.DpapiProtector()
    blob = p.protect(b"x" * 32, purpose("a"))
    assert p.unprotect(blob, purpose("a")) == b"x" * 32
    with pytest.raises(ProtectError) as e:
        p.unprotect(blob, purpose("b"))
    assert e.value.reason == "unprotect"


@win_only
def test_dpapi_tampered_blob_rejected():
    p = winprotect.DpapiProtector()
    blob = bytearray(p.protect(b"y" * 32, purpose("a")))
    blob[len(blob) // 2] ^= 0x01
    with pytest.raises(ProtectError) as e:
        p.unprotect(bytes(blob), purpose("a"))
    assert e.value.reason == "unprotect"


@win_only
def test_dpapi_blob_is_not_plaintext():
    p = winprotect.DpapiProtector()
    secret = bytes(range(32))
    assert secret not in p.protect(secret, purpose("a"))


def test_default_protector_refuses_plain_outside_source(monkeypatch):
    monkeypatch.setattr(winprotect.sys, "platform", "darwin")
    for kind in ("test", "release"):
        with pytest.raises(ProtectError) as e:
            default_protector(kind)
        assert e.value.reason == "unavailable"
    assert isinstance(default_protector("source"), DevPlainProtector)

    class FakeDpapi:
        protected = True

    monkeypatch.setattr(winprotect.sys, "platform", "win32")
    monkeypatch.setattr(winprotect, "DpapiProtector", FakeDpapi)
    for kind in ("source", "test", "release"):
        assert isinstance(default_protector(kind), FakeDpapi)


def test_dev_plain_purpose_mismatch():
    p = DevPlainProtector()
    assert p.protected is False
    blob = p.protect(b"data", purpose("a"))
    assert p.unprotect(blob, purpose("a")) == b"data"
    with pytest.raises(ProtectError) as e:
        p.unprotect(blob, purpose("b"))
    assert e.value.reason == "unprotect"


def test_purpose_parts_are_length_prefixed():
    assert purpose("a", b"bc") != purpose("a", b"b", b"c")
    assert purpose("a") != purpose("b")


def test_atomic_write_replaces_and_cleans_tmp(tmp_path, monkeypatch):
    f = tmp_path / "state.bin"
    atomic_write_bytes(f, b"old")
    atomic_write_bytes(f, b"new")
    assert f.read_bytes() == b"new"

    def boom(*a, **k):
        raise OSError("replace")

    monkeypatch.setattr(winprotect.os, "replace", boom)
    with pytest.raises(OSError):
        atomic_write_bytes(f, b"newer")
    assert f.read_bytes() == b"new"
    assert [p.name for p in tmp_path.iterdir()] == ["state.bin"]


def test_atomic_write_handles_partial_write(tmp_path, monkeypatch):
    real_write = os.write

    def short_write(fd, data):
        return real_write(fd, bytes(data[:3]))

    monkeypatch.setattr(winprotect.os, "write", short_write)
    f = tmp_path / "x.bin"
    atomic_write_bytes(f, b"0123456789")
    assert f.read_bytes() == b"0123456789"


def test_quarantine_renames(tmp_path):
    f = tmp_path / "install.dpapi"
    f.write_bytes(b"junk")
    dest = quarantine(f)
    assert dest is not None and dest.name.startswith("install.dpapi.bad-")
    assert not f.exists() and dest.read_bytes() == b"junk"
    assert quarantine(tmp_path / "none") is None


def test_quarantine_keeps_only_recent_copies(tmp_path):
    p = tmp_path / "bundle-cache.json"
    for i in range(5):
        p.write_text(str(i))
        quarantine(p)
    copies = sorted(tmp_path.glob("bundle-cache.json.bad-*"))
    assert len(copies) == winprotect.QUARANTINE_KEEP == 3
    assert sorted(c.read_text() for c in copies) == ["2", "3", "4"]
    other = tmp_path / "state.dpapi"
    other.write_text("s")
    quarantine(other)
    assert len(list(tmp_path.glob("state.dpapi.bad-*"))) == 1
