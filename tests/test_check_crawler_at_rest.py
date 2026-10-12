"""scripts/check_crawler_at_rest.py 시험(A3 운영 확인기). 합성 행만 쓰고 DB 에 접속하지 않는다."""
import base64
import hashlib
import importlib.util
import json
import os
from pathlib import Path

import pytest

from domae_mcp.cloud import crypto

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_crawler_at_rest.py"


def _load():
    spec = importlib.util.spec_from_file_location("check_crawler_at_rest", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _key() -> str:
    return base64.b64encode(os.urandom(32)).decode()


class _Conn:
    def __init__(self, rows):
        self.rows = rows

    def cursor(self):
        rows = self.rows

        class _C:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def execute(self, sql):
                assert "domae_crawlers" in sql

            def fetchall(self):
                return rows
        return _C()

    def close(self):
        pass


def _row(name, src):
    return name, crypto.encrypt_crawler_code(src), hashlib.sha256(src.encode()).hexdigest()


def _run(monkeypatch, capsys, rows, argv=()):
    mod = _load()
    monkeypatch.setenv("DATABASE_URL", "postgresql://unused")
    monkeypatch.setattr(mod.psycopg2, "connect", lambda url: _Conn(rows))
    rc = mod.main(list(argv))
    return rc, json.loads(capsys.readouterr().out)


def test_rotation_report_and_restore_decision(monkeypatch, capsys):
    old, new = _key(), _key()
    monkeypatch.delenv("DOMAE_CRAWLER_KEY_PREVIOUS", raising=False)
    monkeypatch.setenv("DOMAE_CRAWLER_KEY", old)
    old_rows = [_row("synth_a", "a = 1"), _row("synth_b", "b = 2")]
    monkeypatch.setenv("DOMAE_CRAWLER_KEY", new)
    monkeypatch.setenv("DOMAE_CRAWLER_KEY_PREVIOUS", old)
    monkeypatch.setenv("DOMAE_CREDENTIAL_KEY", old)
    rc, out = _run(monkeypatch, capsys, old_rows)
    assert rc == 0 and out["previousUsed"] == ["synth_a", "synth_b"] and out["canRestoreOldKeyOnly"] is True
    reseeded = _row("synth_b", "b = 2")  # 교체 중 재시딩된 행(새 키)
    rc, out = _run(monkeypatch, capsys, [old_rows[0], reseeded])
    assert out["currentUsed"] == ["synth_b"] and out["canRestoreOldKeyOnly"] is False
    rc, out = _run(monkeypatch, capsys, [old_rows[0], reseeded], ["--final"])
    assert rc == 4


def test_final_passes_only_after_rotation(monkeypatch, capsys):
    key = _key()
    monkeypatch.delenv("DOMAE_CRAWLER_KEY_PREVIOUS", raising=False)
    monkeypatch.setenv("DOMAE_CRAWLER_KEY", key)
    monkeypatch.setenv("DOMAE_CREDENTIAL_KEY", _key())
    rc, out = _run(monkeypatch, capsys, [_row("synth_a", "a = 1")], ["--final"])
    assert rc == 0 and out["sameAsCredential"] is False and out["previousConfigured"] is False


def test_unreadable_row_and_missing_key(monkeypatch, capsys):
    monkeypatch.delenv("DOMAE_CRAWLER_KEY_PREVIOUS", raising=False)
    monkeypatch.setenv("DOMAE_CRAWLER_KEY", _key())
    rc, out = _run(monkeypatch, capsys, [("synth_x", "v1:" + base64.b64encode(os.urandom(40)).decode(), "0" * 64)])
    assert rc == 1 and out["failed"] == ["synth_x"]
    monkeypatch.delenv("DOMAE_CRAWLER_KEY")
    rc, out = _run(monkeypatch, capsys, [])
    assert rc == 2 and out == {"key": "missing"}
