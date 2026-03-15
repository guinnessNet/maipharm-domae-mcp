"""API 키 검증 및 하트비트: 팜스퀘어 API 키 기반 인증"""

import logging
from datetime import datetime

import httpx

from domae_mcp import __version__
from domae_mcp.local.config import ConfigManager

logger = logging.getLogger(__name__)

VERIFY_URL = "https://api.domae.kr/api/verify"
HEARTBEAT_URL = "https://api.domae.kr/api/heartbeat"
OFFLINE_GRACE_DAYS = 7


class ApiKeyManager:
    """API 키 검증 및 기능 제어.

    - 앱 시작 시 verify()로 서버 검증
    - 검증 실패 시 캐시 확인 (오프라인 7일 유예)
    - 주기적 heartbeat()로 사용 통계 전송 (실패 무시)
    """

    def __init__(self, config: ConfigManager):
        self._config = config

    async def verify(self, api_key: str) -> dict:
        """API 키 서버 검증. 실패 시 캐시 확인.

        Args:
            api_key: dmk_free_... 형태의 API 키.

        Returns:
            검증 결과 dict. 예:
            {
                "valid": True,
                "tier": "free",
                "pharmacy_name": "마이약국",
                "features": {
                    "min_interval": 60,
                    "max_crawlers": 8,
                    "telegram": True,
                    "kakao": False,
                },
            }

        Raises:
            ValueError: API 키가 유효하지 않고 캐시도 만료된 경우.
        """
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    VERIFY_URL,
                    params={"key": api_key},
                    timeout=5.0,
                )

            if resp.status_code == 200:
                data = resp.json()
                if data.get("valid"):
                    # 검증 성공 → 캐시 갱신
                    self._config._config["api_key_verified_at"] = datetime.now().isoformat()
                    self._config._save()
                    logger.info("API 키 검증 성공: %s", data.get("pharmacy_name", ""))
                    return data

            # 서버가 거부한 경우 (200이 아니거나 valid=false)
            raise ValueError("유효하지 않은 API 키입니다. 팜스퀘어에서 발급받으세요.")

        except httpx.HTTPError as e:
            # 네트워크 오류 → 캐시 확인
            logger.warning("API 키 검증 서버 연결 실패: %s", e)
            if self.is_valid_cached():
                logger.info("오프라인 모드: 캐시된 검증 결과 사용 (7일 유예)")
                return {
                    "valid": True,
                    "tier": "free",
                    "pharmacy_name": "",
                    "features": {
                        "min_interval": 60,
                        "max_crawlers": 8,
                        "telegram": True,
                        "kakao": False,
                    },
                    "offline": True,
                }
            raise ValueError(
                "API 키 검증 서버에 연결할 수 없고, 캐시된 검증 결과도 만료되었습니다. "
                "인터넷 연결을 확인하세요."
            ) from e

    def is_valid_cached(self) -> bool:
        """오프라인 시 캐시된 검증 결과 확인.

        마지막 검증 성공 후 OFFLINE_GRACE_DAYS 이내이면 유효.
        """
        cached = self._config._config.get("api_key_verified_at")
        if not cached:
            return False
        try:
            verified_at = datetime.fromisoformat(cached)
            return (datetime.now() - verified_at).days < OFFLINE_GRACE_DAYS
        except (ValueError, TypeError):
            return False

    async def heartbeat(self, api_key: str, stats: dict) -> None:
        """주기적 하트비트 전송 (12시간마다). 실패해도 무시.

        Args:
            api_key: API 키.
            stats: 사용 통계 dict. 예:
                {
                    "search_count": 42,
                    "order_count": 5,
                    "active_monitors": 3,
                }
        """
        try:
            async with httpx.AsyncClient() as client:
                await client.post(
                    HEARTBEAT_URL,
                    json={
                        "key": api_key,
                        "version": __version__,
                        "search_count": stats.get("search_count", 0),
                        "order_count": stats.get("order_count", 0),
                        "active_monitors": stats.get("active_monitors", 0),
                    },
                    timeout=5.0,
                )
            logger.debug("하트비트 전송 완료")
        except Exception:
            # 하트비트 실패는 무시 (오프라인 동작 가능)
            logger.debug("하트비트 전송 실패 (무시)", exc_info=True)
