"""로컬 비밀 보호 저장 시험 (계획 Task 9, 스펙 B10). 비밀 값은 시험용 합성 문자열만 쓴다."""
import json
import os
import stat
import sys

import pytest

from domae_mcp.core.crawlers.registry import CrawlerRegistry
from domae_mcp.local.config import ConfigManager
from domae_mcp.v2.artifacts import unload_all
from domae_mcp.v2.client import V2Client
from domae_mcp.v2.encoding import key_fp_from_bearer
from domae_mcp.v2.gate import RuntimeGate
from domae_mcp.v2.keyslot import KeySlots
from domae_mcp.v2.loader import V2Loader
from domae_mcp.v2.runtime import local_runtime_id
from domae_mcp.v2.store import StateStore
from domae_mcp.v2.winprotect import DevPlainProtector, purpose
from v2_fakes import (DEVICE_HEX, TEST_BEARER, TEST_BEARER_2, TEST_TRUST, FakeTime, FakeV2Server, T0,
                      fake_artifacts, pick_protector)

PW = "pw-SYNTHETIC-7f3a9c"
TOKEN = "123456:SYNTHETIC-telegram-token-xyz"
win_only = pytest.mark.skipif(sys.platform != "win32", reason="DPAPI")


@pytest.fixture
def home(tmp_path):
    return tmp_path / "home"


def _cfg(home, prot=None):
    return ConfigManager(base_dir=home, protector=prot or pick_protector())


def _all_bytes(root):
    for p in root.rglob("*"):
        if p.is_file():
            yield p, p.read_bytes()


def test_secrets_not_in_config_json(home):
    c = _cfg(home)
    c.set_credentials("지오영", "user-1", PW)
    c.set_telegram(TOKEN, "-1000000000001")
    raw = (home / "config.json").read_text(encoding="utf-8")
    data = json.loads(raw)
    assert PW not in raw and TOKEN not in raw
    assert data["credentials"]["지오영"] == {"login_id": "user-1"}
    assert data["telegram"] == {"chat_id": "-1000000000001"}
    assert "api_key" not in data
    again = _cfg(home)
    assert again.get_credentials("지오영") == {"login_id": "user-1", "login_pw": PW}
    assert again.get_telegram() == {"token": TOKEN, "chat_id": "-1000000000001"}


@win_only
def test_no_plaintext_in_app_home_dpapi(home):
    c = _cfg(home)
    c.set_credentials("복산", "user-2", PW)
    c.set_telegram(TOKEN, "-1000000000002")
    slots = KeySlots(home / "v2", c._prot)
    slots.stage(TEST_BEARER)
    slots.commit(key_fp_from_bearer(TEST_BEARER))
    assert c.get_api_key() == TEST_BEARER
    needles = [s.encode(enc) for s in (PW, TOKEN, TEST_BEARER) for enc in ("utf-8", "utf-16-le")]
    for p, data in _all_bytes(home):
        for n in needles:
            assert n not in data, p.name


@pytest.mark.skipif(sys.platform == "win32", reason="비 Windows source 전용")
def test_dev_plain_permissions_and_warning(home):
    c = ConfigManager(base_dir=home)  # 비 Windows source → DevPlainProtector
    assert c.protected is False
    c.set_credentials("인천", "user-3", PW)
    mode = stat.S_IMODE(os.stat(home / "v2" / "secrets.dpapi").st_mode)
    assert mode == 0o600


def test_dev_plain_reports_unprotected(home, monkeypatch, caplog):
    import domae_mcp.local.config as cfg_mod
    monkeypatch.setattr(cfg_mod, "_warned_unprotected", False)
    with caplog.at_level("WARNING", logger="domae_mcp.local.config"):
        c = _cfg(home, DevPlainProtector())
    assert c.protected is False
    assert any("보호되지 않습니다" in r.getMessage() for r in caplog.records)


def test_locked_secrets_file_is_not_overwritten(home, monkeypatch):
    c = _cfg(home)
    c.set_credentials("지오영", "user-a", PW)
    path = home / "v2" / "secrets.dpapi"
    real = type(path).read_bytes

    def locked(self):
        if self.name == "secrets.dpapi":
            raise PermissionError("locked")
        return real(self)

    monkeypatch.setattr(type(path), "read_bytes", locked)
    with pytest.raises(OSError):
        c.set_credentials("복산", "user-b", "other-pw")
    monkeypatch.undo()
    assert _cfg(home).get_credentials("지오영")["login_pw"] == PW


def test_quarantine_seen_by_other_instance(home):
    writer = _cfg(home)
    writer.set_credentials("백제", "user-q", PW)
    settings_view = _cfg(home)  # 설정 라우터 인스턴스
    (home / "v2" / "secrets.dpapi").write_bytes(b"corrupted-blob")
    search_view = _cfg(home)  # 검색이 먼저 읽어 격리한다
    assert search_view.get_credentials("백제")["login_pw"] == ""
    assert settings_view.reentry_required() == ["credentials:백제"]


def test_bad_credentials_type_does_not_break_import(home):
    home.mkdir(parents=True)
    (home / "config.json").write_text(json.dumps({"credentials": [], "telegram": "x"}), encoding="utf-8")
    c = _cfg(home)
    assert c.get_all_credentials()[0]["configured"] is False


def test_corrupt_config_json_quarantined(home):
    home.mkdir(parents=True)
    (home / "config.json").write_text("{broken", encoding="utf-8")
    c = _cfg(home)
    assert c.get_port() == 5900
    assert [p for p in home.iterdir() if p.name.startswith("config.json.bad-")]


def _write_legacy(home):
    home.mkdir(parents=True, exist_ok=True)
    legacy = {"version": "1.0.0", "port": 5900, "api_key": TEST_BEARER,
              "credentials": {"지오영": {"login_id": "old-id", "login_pw": "gAAAAAB-legacy-fernet"},
                              "복산": {"login_id": "", "login_pw": ""}},
              "telegram": {"token": "gAAAAAB-legacy-token", "chat_id": "-1000000000003"}}
    (home / "config.json").write_text(json.dumps(legacy), encoding="utf-8")
    (home / ".key").write_bytes(b"legacy-fernet-key-material")


def test_legacy_fields_removed_and_reentry_flagged(home):
    _write_legacy(home)
    c = _cfg(home)
    raw = (home / "config.json").read_text(encoding="utf-8")
    for s in (TEST_BEARER, "gAAAAAB-legacy-fernet", "gAAAAAB-legacy-token"):
        assert s not in raw
    data = json.loads(raw)
    assert "api_key" not in data and "login_pw" not in data["credentials"]["지오영"]
    assert data["credentials"]["지오영"]["login_id"] == "old-id"
    assert c.reentry_required() == ["api_key", "credentials:지오영", "telegram"]
    assert c.get_credentials("지오영") == {"login_id": "old-id", "login_pw": ""}  # 옛 값은 읽지 않는다
    c.set_credentials("지오영", "old-id", PW)
    c.set_telegram(TOKEN, "-1000000000003")
    assert c.reentry_required() == ["api_key"]


def test_legacy_key_file_deleted(home):
    _write_legacy(home)
    _cfg(home)
    assert not (home / ".key").exists()


def test_secret_unprotect_failure_requests_reentry(home):
    c = _cfg(home)
    c.set_credentials("백제", "user-4", PW)
    c.set_telegram(TOKEN, "-1000000000004")
    (home / "v2" / "secrets.dpapi").write_bytes(b"corrupted-blob")
    c2 = _cfg(home)
    assert c2.get_credentials("백제")["login_pw"] == ""
    assert set(c2.reentry_required()) == {"credentials:백제", "telegram"}
    assert [p for p in (home / "v2").iterdir() if p.name.startswith("secrets.dpapi.bad-")]
    assert not (home / "v2" / "secrets.dpapi").exists()  # 평문으로 다시 저장하지 않는다


@win_only
def test_other_profile_cannot_read(home):
    c = _cfg(home)
    c.set_credentials("피코", "user-5", PW)
    blob = (home / "v2" / "secrets.dpapi").read_bytes()
    from domae_mcp.v2.winprotect import DpapiProtector, ProtectError
    with pytest.raises(ProtectError):
        DpapiProtector().unprotect(blob, purpose("state"))  # 다른 entropy
    plain_blob = DevPlainProtector().protect(b'{"format":1,"values":{}}', purpose("secrets"))
    with pytest.raises(ProtectError):
        DpapiProtector().unprotect(plain_blob, purpose("secrets"))  # 다른 보호기로 만든 파일


def test_api_key_change_clears_bundle_state(tmp_path):
    home = tmp_path / "home"
    prot = pick_protector()
    root = home / "v2"
    t = FakeTime(T0)
    server = FakeV2Server(fake_artifacts(), now=lambda: t.w)
    state, slots = StateStore(root, prot), KeySlots(root, prot)

    def factory(bearer, still_current, policy_write):
        return V2Loader(bearer, slots.cache_dir(key_fp_from_bearer(bearer)), state, prot,
                        client=V2Client(bearer, DEVICE_HEX, http=server.client(), base="https://pharmsq.com/api/domae"),
                        clock=t.clock(), trust=TEST_TRUST, still_current=still_current, policy_write=policy_write,
                        device_id_hex=DEVICE_HEX, runtime_id=local_runtime_id())

    gate = RuntimeGate(slots, state, prot, clock=t.clock(), loader_factory=factory, runner=lambda fn: None)
    try:
        gate.switch(TEST_BEARER)
        c = ConfigManager(base_dir=home, protector=prot)
        assert c.get_api_key() == TEST_BEARER
        gate.switch(TEST_BEARER_2)
        assert c.get_api_key() == TEST_BEARER_2
        fa = key_fp_from_bearer(TEST_BEARER)
        assert not state.has(fa) and not slots.cache_dir(fa).exists()
    finally:
        CrawlerRegistry.clear()
        unload_all()
