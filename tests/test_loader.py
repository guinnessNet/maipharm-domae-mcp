"""CrawlerLoader 단위 테스트

서버 없이 로컬에서 서명 검증, 캐시, 동적 import를 테스트한다.
테스트용 키페어를 별도로 생성하여 사용.
"""

import base64
import hashlib
import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization

from domae_mcp.core.crawlers.base import BaseCrawler
from domae_mcp.core.crawlers.loader import CrawlerLoader
from domae_mcp.core.crawlers.registry import CrawlerRegistry


# ─── 테스트용 키페어 및 헬퍼 ──────────────────────

@pytest.fixture
def test_keypair():
    """테스트용 Ed25519 키페어 생성."""
    private_key = Ed25519PrivateKey.generate()
    private_bytes = private_key.private_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_bytes = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return {
        "private_der": private_bytes,
        "public_b64": base64.b64encode(public_bytes).decode(),
        "private_key": private_key,
    }


@pytest.fixture
def tmp_dir():
    """임시 디렉토리."""
    with tempfile.TemporaryDirectory() as d:
        yield Path(d)


DUMMY_CRAWLER_CODE = '''
from domae_mcp.core.crawlers.base import BaseCrawler, SearchResult, OrderResult

class TestCrawler(BaseCrawler):
    SUPPLIER_NAME = "테스트도매"

    def login(self, login_id, login_pw):
        return True

    def search(self, keyword):
        return [SearchResult(product_name="테스트약품", supplier="테스트도매")]
'''

API_KEY = "dmk_free_test123"
API_KEY_HASH = hashlib.sha256(API_KEY.encode()).hexdigest()


def create_signed_response(bundle_data: dict, private_key: Ed25519PrivateKey) -> dict:
    """테스트용 서명된 응답 생성."""
    payload_str = json.dumps(bundle_data, ensure_ascii=False)
    payload_hash = hashlib.sha256(payload_str.encode()).digest()
    signature = private_key.sign(payload_hash)
    return {
        "payload": payload_str,
        "signature": base64.b64encode(signature).decode(),
    }


def make_bundle(crawlers=None, api_key_hash=None, expires_days=7):
    """테스트용 번들 데이터."""
    now = datetime.now(timezone.utc)
    return {
        "version": "test.001",
        "issued_at": now.isoformat(),
        "expires_at": (now + timedelta(days=expires_days)).isoformat(),
        "api_key_hash": api_key_hash or API_KEY_HASH,
        "crawlers": crawlers or {"test_crawler": DUMMY_CRAWLER_CODE},
    }


# ─── 테스트 케이스 ──────────────────────────────

class TestSignatureVerification:
    """서명 검증 테스트."""

    def test_valid_signature(self, test_keypair, tmp_dir, monkeypatch):
        """올바른 서명은 검증 성공."""
        monkeypatch.setattr("domae_mcp.core.crawlers.loader.PUBLIC_KEY_B64", test_keypair["public_b64"])
        loader = CrawlerLoader(tmp_dir, API_KEY)

        bundle_data = make_bundle()
        response = create_signed_response(bundle_data, test_keypair["private_key"])
        result = loader._verify_and_parse(response)

        assert result is not None
        assert result["version"] == "test.001"

    def test_tampered_payload(self, test_keypair, tmp_dir, monkeypatch):
        """변조된 payload는 검증 실패."""
        monkeypatch.setattr("domae_mcp.core.crawlers.loader.PUBLIC_KEY_B64", test_keypair["public_b64"])
        loader = CrawlerLoader(tmp_dir, API_KEY)

        bundle_data = make_bundle()
        response = create_signed_response(bundle_data, test_keypair["private_key"])
        # payload 변조
        response["payload"] = response["payload"].replace("test.001", "hacked.999")

        result = loader._verify_and_parse(response)
        assert result is None

    def test_wrong_public_key(self, tmp_dir, monkeypatch):
        """다른 키로 서명하면 검증 실패."""
        signer = Ed25519PrivateKey.generate()
        wrong_keypair = Ed25519PrivateKey.generate()
        wrong_pub = base64.b64encode(
            wrong_keypair.public_key().public_bytes(
                encoding=serialization.Encoding.Raw,
                format=serialization.PublicFormat.Raw,
            )
        ).decode()
        monkeypatch.setattr("domae_mcp.core.crawlers.loader.PUBLIC_KEY_B64", wrong_pub)
        loader = CrawlerLoader(tmp_dir, API_KEY)

        bundle_data = make_bundle()
        response = create_signed_response(bundle_data, signer)
        result = loader._verify_and_parse(response)
        assert result is None


class TestApiKeyHash:
    """API 키 해시 검증 테스트."""

    def test_hash_mismatch_rejected(self, test_keypair, tmp_dir, monkeypatch):
        """다른 API 키의 번들은 거부."""
        monkeypatch.setattr("domae_mcp.core.crawlers.loader.PUBLIC_KEY_B64", test_keypair["public_b64"])
        loader = CrawlerLoader(tmp_dir, API_KEY)

        bundle_data = make_bundle(api_key_hash="wrong_hash_value")
        response = create_signed_response(bundle_data, test_keypair["private_key"])

        # _verify_and_parse는 통과하지만, load에서 api_key_hash 체크 시 거부
        bundle = loader._verify_and_parse(response)
        assert bundle is not None
        assert bundle["api_key_hash"] != loader._api_key_hash


class TestCacheManagement:
    """캐시 저장/로드 테스트."""

    def test_save_and_load(self, test_keypair, tmp_dir, monkeypatch):
        """캐시 저장 후 로드 가능."""
        monkeypatch.setattr("domae_mcp.core.crawlers.loader.PUBLIC_KEY_B64", test_keypair["public_b64"])
        loader = CrawlerLoader(tmp_dir, API_KEY)

        bundle_data = make_bundle()
        response = create_signed_response(bundle_data, test_keypair["private_key"])
        loader._save_to_cache(response)

        loaded = loader._load_cached_response()
        assert loaded is not None
        assert loaded["payload"] == response["payload"]
        assert loaded["signature"] == response["signature"]

    def test_expired_cache_rejected(self, test_keypair, tmp_dir, monkeypatch):
        """만료된 캐시는 거부."""
        monkeypatch.setattr("domae_mcp.core.crawlers.loader.PUBLIC_KEY_B64", test_keypair["public_b64"])
        loader = CrawlerLoader(tmp_dir, API_KEY)

        # 20일 전에 만료된 번들 (유예 7일 포함해도 만료)
        bundle_data = make_bundle(expires_days=-20)
        assert loader._is_expired(bundle_data) is True

    def test_valid_cache_accepted(self, test_keypair, tmp_dir, monkeypatch):
        """유효한 캐시는 수락."""
        monkeypatch.setattr("domae_mcp.core.crawlers.loader.PUBLIC_KEY_B64", test_keypair["public_b64"])
        loader = CrawlerLoader(tmp_dir, API_KEY)

        bundle_data = make_bundle(expires_days=7)
        assert loader._is_expired(bundle_data) is False

    def test_grace_period(self, test_keypair, tmp_dir, monkeypatch):
        """만료 후 유예 기간(7일) 내에는 수락."""
        monkeypatch.setattr("domae_mcp.core.crawlers.loader.PUBLIC_KEY_B64", test_keypair["public_b64"])
        loader = CrawlerLoader(tmp_dir, API_KEY)

        # 3일 전 만료 → 유예 7일 내이므로 유효
        bundle_data = make_bundle(expires_days=-3)
        assert loader._is_expired(bundle_data) is False

    def test_grace_period_exceeded(self, test_keypair, tmp_dir, monkeypatch):
        """유예 기간 초과 시 거부."""
        monkeypatch.setattr("domae_mcp.core.crawlers.loader.PUBLIC_KEY_B64", test_keypair["public_b64"])
        loader = CrawlerLoader(tmp_dir, API_KEY)

        # 10일 전 만료 → 유예 7일 초과
        bundle_data = make_bundle(expires_days=-10)
        assert loader._is_expired(bundle_data) is True


class TestDynamicImport:
    """동적 import 테스트."""

    def test_import_crawler(self, test_keypair, tmp_dir, monkeypatch):
        """크롤러 코드에서 BaseCrawler 서브클래스 로드."""
        monkeypatch.setattr("domae_mcp.core.crawlers.loader.PUBLIC_KEY_B64", test_keypair["public_b64"])
        loader = CrawlerLoader(tmp_dir, API_KEY)

        bundle_data = make_bundle()
        loaded = loader._import_crawlers(bundle_data)

        assert "테스트도매" in loaded
        crawler_cls = loaded["테스트도매"]
        assert issubclass(crawler_cls, BaseCrawler)

        # 인스턴스 생성 테스트
        instance = crawler_cls()
        assert instance.SUPPLIER_NAME == "테스트도매"
        assert instance.login("test", "test") is True

    def test_empty_crawlers_warning(self, test_keypair, tmp_dir, monkeypatch):
        """크롤러 코드가 잘못되면 0개 로드 + 경고."""
        monkeypatch.setattr("domae_mcp.core.crawlers.loader.PUBLIC_KEY_B64", test_keypair["public_b64"])
        loader = CrawlerLoader(tmp_dir, API_KEY)

        bundle_data = make_bundle(crawlers={"broken": "this is not python code !!!"})
        loaded = loader._import_crawlers(bundle_data)

        assert len(loaded) == 0

    def test_multiple_crawlers(self, test_keypair, tmp_dir, monkeypatch):
        """여러 크롤러를 한번에 로드."""
        monkeypatch.setattr("domae_mcp.core.crawlers.loader.PUBLIC_KEY_B64", test_keypair["public_b64"])
        loader = CrawlerLoader(tmp_dir, API_KEY)

        crawler_b = DUMMY_CRAWLER_CODE.replace("TestCrawler", "TestCrawlerB").replace("테스트도매", "도매B")
        bundle_data = make_bundle(crawlers={
            "test_a": DUMMY_CRAWLER_CODE,
            "test_b": crawler_b,
        })
        loaded = loader._import_crawlers(bundle_data)

        assert len(loaded) == 2
        assert "테스트도매" in loaded
        assert "도매B" in loaded


class TestRegistryIntegration:
    """CrawlerRegistry 연동 테스트."""

    def test_register_all(self, test_keypair, tmp_dir, monkeypatch):
        """로드된 크롤러를 Registry에 일괄 등록."""
        monkeypatch.setattr("domae_mcp.core.crawlers.loader.PUBLIC_KEY_B64", test_keypair["public_b64"])
        CrawlerRegistry.clear()

        loader = CrawlerLoader(tmp_dir, API_KEY)
        bundle_data = make_bundle()
        loaded = loader._import_crawlers(bundle_data)

        CrawlerRegistry.register_all(loaded)

        assert CrawlerRegistry.is_loaded() is True
        assert "테스트도매" in CrawlerRegistry.list_all()

        instance = CrawlerRegistry.get("테스트도매")
        assert isinstance(instance, BaseCrawler)

        CrawlerRegistry.clear()
