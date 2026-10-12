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
