import base64
import os

import pytest

from domae_mcp.cloud import crypto


def _key() -> str:
    return base64.b64encode(os.urandom(32)).decode()


def test_roundtrip_with_crawler_key(monkeypatch):
    monkeypatch.setenv("DOMAE_CRAWLER_KEY", _key())
    monkeypatch.delenv("DOMAE_CREDENTIAL_KEY", raising=False)
    assert crypto.decrypt_crawler_code(crypto.encrypt_crawler_code("print(1)")) == "print(1)"


def test_no_fallback_to_credential_key(monkeypatch):
    monkeypatch.delenv("DOMAE_CRAWLER_KEY", raising=False)
    monkeypatch.setenv("DOMAE_CREDENTIAL_KEY", _key())
    with pytest.raises(RuntimeError, match="DOMAE_CRAWLER_KEY"):
        crypto.encrypt_crawler_code("print(1)")


def test_legacy_plaintext_rejected(monkeypatch):
    monkeypatch.setenv("DOMAE_CRAWLER_KEY", _key())
    with pytest.raises(RuntimeError, match="legacy plaintext crawler code is not allowed"):
        crypto.decrypt_crawler_code("print(1)")


def test_previous_key_decrypts_only(monkeypatch):
    old, new = _key(), _key()
    monkeypatch.setenv("DOMAE_CRAWLER_KEY", old)
    monkeypatch.delenv("DOMAE_CRAWLER_KEY_PREVIOUS", raising=False)
    stored_old = crypto.encrypt_crawler_code("print(2)")
    monkeypatch.setenv("DOMAE_CRAWLER_KEY", new)
    monkeypatch.setenv("DOMAE_CRAWLER_KEY_PREVIOUS", old)
    assert crypto.decrypt_crawler_code(stored_old) == "print(2)"
    stored_new = crypto.encrypt_crawler_code("print(3)")
    monkeypatch.delenv("DOMAE_CRAWLER_KEY_PREVIOUS")
    assert crypto.decrypt_crawler_code(stored_new) == "print(3)"  # 새 암호문은 현재 키로만 열린다
    monkeypatch.setenv("DOMAE_CRAWLER_KEY", old)
    with pytest.raises(Exception):
        crypto.decrypt_crawler_code(stored_new)


def test_without_previous_other_key_fails(monkeypatch):
    monkeypatch.setenv("DOMAE_CRAWLER_KEY", _key())
    monkeypatch.delenv("DOMAE_CRAWLER_KEY_PREVIOUS", raising=False)
    stored = crypto.encrypt_crawler_code("x")
    monkeypatch.setenv("DOMAE_CRAWLER_KEY", _key())
    with pytest.raises(Exception):
        crypto.decrypt_crawler_code(stored)


def test_previous_key_wrong_length(monkeypatch):
    monkeypatch.setenv("DOMAE_CRAWLER_KEY", _key())
    monkeypatch.setenv("DOMAE_CRAWLER_KEY_PREVIOUS", base64.b64encode(os.urandom(16)).decode())
    with pytest.raises(RuntimeError, match="PREVIOUS"):
        crypto.decrypt_crawler_code(crypto.CRAWLER_MAGIC_PREFIX + base64.b64encode(os.urandom(40)).decode())


def test_credential_key_is_never_previous(monkeypatch):
    """DOMAE_CREDENTIAL_KEY 는 이전 키로도 쓰이지 않는다(명시한 DOMAE_CRAWLER_KEY_PREVIOUS 만)."""
    cred = _key()
    monkeypatch.setenv("DOMAE_CRAWLER_KEY", cred)
    stored = crypto.encrypt_crawler_code("y")
    monkeypatch.setenv("DOMAE_CRAWLER_KEY", _key())
    monkeypatch.setenv("DOMAE_CREDENTIAL_KEY", cred)
    monkeypatch.delenv("DOMAE_CRAWLER_KEY_PREVIOUS", raising=False)
    with pytest.raises(Exception):
        crypto.decrypt_crawler_code(stored)
