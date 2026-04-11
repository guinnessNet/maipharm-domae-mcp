"""크롤러 서버 배포 로더: 서버에서 크롤러 번들을 다운로드하고 검증/캐시/로드

서버 응답 구조: {"payload": "JSON문자열", "signature": "base64"}
payload를 재직렬화하지 않고 원문 바이트의 SHA256을 서명 검증한다.
"""

import base64
import hashlib
import importlib.util
import json
import logging
import os
import sys
import tempfile
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from domae_mcp.core.crawlers.base import BaseCrawler
from domae_mcp.core.crawlers._client_key import _CRAWLER_CLIENT_KEY_B64

logger = logging.getLogger(__name__)

# Step 1.5 — 서버가 번들 내 크롤러 코드를 이 prefix로 암호화해서 보낼 때 메모리 복호화.
_ENCRYPTED_PREFIX = "v1:"

# ─── 상수 ──────────────────────────────────────────
SERVER_URL = "https://api.pharmsq.com/api/domae"
CACHE_DIR_NAME = "crawlers"
BUNDLE_FILE = "bundle.json"
OFFLINE_GRACE_DAYS = 7

# Ed25519 공개키 (공개키이므로 소스코드에 포함해도 안전)
PUBLIC_KEY_B64 = "ZHEtVBWCnETR8GnOVNDY+TP8NgPYpMEIc+aX2euGcbA="


class CrawlerLoader:
    """서버에서 크롤러 코드를 받아 로컬에서 실행.

    흐름:
    1. load() 호출 (동기 — MCP 모드 호환)
    2. 캐시에 유효한 번들이 있으면 캐시에서 로드
    3. 없거나 만료되었으면 서버에서 다운로드
    4. 서명 검증 → 캐시 저장 → 동적 import → 크롤러 클래스 반환

    async 컨텍스트(FastAPI lifespan)에서는 asyncio.to_thread(loader.load)로 호출.
    """

    def __init__(self, base_dir: Path, api_key: str):
        """
        Args:
            base_dir: ~/.maipharm-domae-mcp/ 경로 (ConfigManager.base_dir)
            api_key: "dmk_free_xxx" 형태의 API 키
        """
        self._base_dir = base_dir
        self._api_key = api_key
        self._cache_dir = base_dir / CACHE_DIR_NAME
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._bundle_path = self._cache_dir / BUNDLE_FILE
        self._api_key_hash = hashlib.sha256(api_key.encode()).hexdigest()

    def load(self) -> dict[str, type[BaseCrawler]]:
        """크롤러 클래스들을 로드하여 반환.

        Returns:
            {"지오영": GeoCrawler, "복산": BoksanCrawler, ...}

        Raises:
            RuntimeError: 서버 연결 실패 + 캐시도 만료된 경우
        """
        # 1. 서버에서 최신 번들 시도
        bundle = self._fetch_from_server()

        if bundle is None:
            # 2. 서버 실패 → 캐시 사용
            bundle = self._load_from_cache()

        if bundle is None:
            raise RuntimeError(
                "크롤러를 로드할 수 없습니다. "
                "인터넷 연결을 확인하고 API 키가 유효한지 확인하세요."
            )

        # 3. 크롤러 코드를 파일로 저장 + 동적 import
        result = self._import_crawlers(bundle)

        # 번들에 크롤러가 있는데 0개 로드 시 경고
        expected = len(bundle.get("crawlers", {}))
        if expected > 0 and len(result) == 0:
            logger.error("번들에 %d개 크롤러가 있지만 모두 import 실패!", expected)

        return result

    def check_update(self) -> bool:
        """서버에 버전 변경 여부만 확인 (가벼운 체크)."""
        try:
            resp = httpx.get(
                f"{SERVER_URL}/crawlers/version",
                headers={"Authorization": f"Bearer {self._api_key}"},
                timeout=5.0,
            )
            if resp.status_code == 200:
                server_version = resp.json().get("version")
                cached_response = self._load_cached_response()
                if cached_response:
                    cached_bundle = self._verify_and_parse(cached_response)
                    if cached_bundle and cached_bundle.get("version") == server_version:
                        return False  # 변경 없음
                return True  # 새 버전 있음
        except Exception:
            pass
        return False

    # ─── 서버 통신 ──────────────────────────────

    def _fetch_from_server(self) -> Optional[dict]:
        """서버에서 크롤러 번들 다운로드 + 서명 검증."""
        try:
            # 캐시된 response를 파싱한 뒤 만료 체크
            cached_response = self._load_cached_response()
            if cached_response:
                cached_bundle = self._verify_and_parse(cached_response)
                if cached_bundle and not self._is_expired(cached_bundle):
                    if not self.check_update():
                        logger.debug("크롤러 캐시 유효, 서버 동일 버전")
                        return cached_bundle

            # 전체 번들 다운로드 (Step 1.5 — 암호화 번들 요청)
            resp = httpx.get(
                f"{SERVER_URL}/crawlers",
                params={"encrypted": "1"},
                headers={"Authorization": f"Bearer {self._api_key}"},
                timeout=30.0,
            )

            if resp.status_code == 401:
                logger.error("API 키가 유효하지 않습니다.")
                return None
            if resp.status_code == 403:
                logger.error("API 키가 비활성화되었습니다.")
                return None
            if resp.status_code != 200:
                logger.warning("서버 응답 오류: %d", resp.status_code)
                return None

            response = resp.json()

            # 서명 검증 (payload 원문 바이트 사용)
            bundle = self._verify_and_parse(response)
            if bundle is None:
                logger.error("번들 서명 검증 실패 — 변조 가능성")
                return None

            # API 키 해시 검증
            if bundle.get("api_key_hash") != self._api_key_hash:
                logger.error("번들이 이 API 키용이 아닙니다")
                return None

            # 캐시에 atomic write
            self._save_to_cache(response)
            logger.info("크롤러 번들 다운로드 완료 (v%s)", bundle.get("version"))
            return bundle

        except httpx.HTTPError as e:
            logger.warning("서버 연결 실패: %s", e)
            return None

    # ─── 서명 검증 ──────────────────────────────

    def _verify_and_parse(self, response: dict) -> Optional[dict]:
        """서버 응답의 서명을 검증하고 번들 데이터를 파싱.

        response 구조: {"payload": "JSON문자열", "signature": "base64"}
        payload 원문 바이트를 그대로 SHA256하여 검증 (재직렬화 안 함).
        """
        try:
            payload_str = response.get("payload")
            signature_b64 = response.get("signature")
            if not payload_str or not signature_b64:
                return None

            # 원문 바이트의 SHA256 해시
            payload_hash = hashlib.sha256(payload_str.encode()).digest()
            signature = base64.b64decode(signature_b64)
            public_key = Ed25519PublicKey.from_public_bytes(
                base64.b64decode(PUBLIC_KEY_B64)
            )

            public_key.verify(signature, payload_hash)

            # 검증 통과 → JSON 파싱
            return json.loads(payload_str)

        except Exception as e:
            logger.error("서명 검증 에러: %s", e)
            return None

    # ─── 캐시 관리 (atomic write) ──────────────────

    def _save_to_cache(self, response: dict) -> None:
        """번들을 로컬 캐시에 atomic write (temp → rename)."""
        content = json.dumps(response, ensure_ascii=False)
        fd, tmp_path = tempfile.mkstemp(dir=str(self._cache_dir), suffix=".tmp")
        try:
            os.write(fd, content.encode("utf-8"))
        finally:
            os.close(fd)
        try:
            os.replace(tmp_path, str(self._bundle_path))
        except Exception:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
            raise

    def _load_from_cache(self) -> Optional[dict]:
        """캐시에서 유효한 번들 로드. 만료 시 None."""
        cached_response = self._load_cached_response()
        if cached_response is None:
            return None

        bundle = self._verify_and_parse(cached_response)
        if bundle is None:
            logger.warning("캐시된 번들 서명 검증 실패")
            return None

        if self._is_expired(bundle):
            logger.warning("캐시된 크롤러 번들이 만료되었습니다.")
            return None

        logger.info("오프라인 모드: 캐시된 크롤러 사용 (v%s)", bundle.get("version"))
        return bundle

    def _load_cached_response(self) -> Optional[dict]:
        """캐시 파일 읽기 (만료/서명 체크 안 함)."""
        if not self._bundle_path.exists():
            return None
        try:
            return json.loads(self._bundle_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

    def _is_expired(self, bundle: dict) -> bool:
        """번들 만료 여부. expires_at + OFFLINE_GRACE_DAYS 적용."""
        expires_at_str = bundle.get("expires_at")
        if not expires_at_str:
            return True
        try:
            expires_at = datetime.fromisoformat(expires_at_str.replace("Z", "+00:00"))
            grace_deadline = expires_at + timedelta(days=OFFLINE_GRACE_DAYS)
            return datetime.now(timezone.utc) > grace_deadline
        except (ValueError, TypeError):
            return True

    # ─── 동적 import ──────────────────────────────

    def _decrypt_client_ciphertext(self, stored: str) -> str:
        """번들 내 크롤러 코드가 "v1:" prefix로 암호화돼 있으면 CLIENT_KEY로 복호화.
        prefix 없으면 legacy 평문으로 간주하고 그대로 반환 (하위호환).
        """
        if not stored.startswith(_ENCRYPTED_PREFIX):
            return stored
        key = base64.b64decode(_CRAWLER_CLIENT_KEY_B64)
        if len(key) != 32:
            raise RuntimeError("로컬 MCP 크롤러 client 키가 32바이트가 아닙니다.")
        aesgcm = AESGCM(key)
        raw = base64.b64decode(stored[len(_ENCRYPTED_PREFIX):])
        if len(raw) < 12 + 16:
            raise RuntimeError("크롤러 암호문 길이 부족")
        nonce = raw[:12]
        ciphertext = raw[12:]
        plaintext = aesgcm.decrypt(nonce, ciphertext, None)
        return plaintext.decode("utf-8")

    def _import_crawlers(self, bundle: dict) -> dict[str, type[BaseCrawler]]:
        """번들의 크롤러 코드를 **메모리에서** 복호화 + exec하여 동적 import.

        Step 1.5 변경: 디스크에 .py 파일을 쓰지 않음 (crawlers 캐시 디렉토리에 .py 잔존 방지).
        bundle.json 자체는 여전히 atomic write로 캐시되지만, encrypted=True면 내부 code 값은
        "v1:..." 암호문이라 메모장으로 열어도 Python 소스가 드러나지 않음.

        크롤러 코드의 하드 요구사항:
        1. from domae_mcp.core.crawlers.base import BaseCrawler, SearchResult, OrderResult 사용
        2. domae_mcp 패키지가 pip install 되어 있어야 import 성공
        3. 상대 import 사용 금지
        4. requirements.txt에 포함된 패키지만 사용 가능
        """
        crawlers_code = bundle.get("crawlers", {})
        loaded = {}

        for module_name, raw_code in crawlers_code.items():
            try:
                # 1) 서버가 암호화해 보냈으면 메모리에서만 복호화
                plain_code = self._decrypt_client_ciphertext(raw_code)

                # 2) 캐시 디렉토리에 남아있을 수 있는 레거시 .py 파일 제거
                legacy_file = self._cache_dir / f"{module_name}.py"
                if legacy_file.exists():
                    try:
                        legacy_file.unlink()
                    except OSError as e:
                        logger.warning("레거시 .py 제거 실패 [%s]: %s", module_name, e)

                # 3) 메모리에서 동적 모듈 생성 → exec
                module_fqn = f"domae_crawlers.{module_name}"
                module = types.ModuleType(module_fqn)
                module.__file__ = f"<encrypted:{module_name}>"
                module.__loader__ = None
                module.__spec__ = None
                sys.modules[module_fqn] = module
                compiled = compile(plain_code, f"<crawler:{module_name}>", "exec")
                exec(compiled, module.__dict__)

                # 4) BaseCrawler 서브클래스 찾기
                for attr_name in dir(module):
                    attr = getattr(module, attr_name)
                    if (
                        isinstance(attr, type)
                        and issubclass(attr, BaseCrawler)
                        and attr is not BaseCrawler
                    ):
                        supplier_name = getattr(attr, "SUPPLIER_NAME", module_name)
                        loaded[supplier_name] = attr
                        logger.debug("크롤러 로드: %s (%s)", supplier_name, module_name)
                        break

            except Exception as e:
                logger.error("크롤러 로드 실패 [%s]: %s", module_name, e)

        logger.info("크롤러 %d/%d개 로드 완료 (메모리 import)", len(loaded), len(crawlers_code))
        return loaded
