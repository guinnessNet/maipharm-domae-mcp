"""모니터링 제어 라우터: 시작/중지/상태"""

from typing import Optional

from fastapi import APIRouter
from pydantic import BaseModel

from domae_mcp.core.services.monitor_service import MonitorService
from domae_mcp.core.services.telegram_service import TelegramService
from domae_mcp.local.config import ConfigManager, SUPPLIERS
from domae_mcp.local.database import _get_session_factory

router = APIRouter(tags=["모니터링"])

_config = ConfigManager()

# MonitorService 싱글톤 (lazy init)
_monitor_service: Optional[MonitorService] = None


def _get_monitor_service() -> MonitorService:
    """MonitorService 싱글톤 반환. 최신 credentials/telegram 반영."""
    global _monitor_service

    # credentials 구성
    credentials: dict[str, dict[str, str]] = {}
    for sup in SUPPLIERS:
        cred = _config.get_credentials(sup)
        if cred.get("login_id") and cred.get("login_pw"):
            credentials[sup] = cred

    # 텔레그램 설정
    tg = _config.get_telegram()
    telegram_service = TelegramService(token=tg.get("token"), chat_id=tg.get("chat_id"))

    if _monitor_service is None:
        session_factory = _get_session_factory()
        _monitor_service = MonitorService(
            db_session_factory=session_factory,
            credentials=credentials,
            telegram_service=telegram_service,
        )
    else:
        # 런타임에 최신 설정 반영
        _monitor_service.update_credentials(credentials)
        _monitor_service.update_telegram(telegram_service)

    return _monitor_service


# ── Response 스키마 ──


class MonitorStatusResponse(BaseModel):
    running: bool
    last_run: Optional[str] = None


class MessageResponse(BaseModel):
    success: bool
    message: str


# ── 엔드포인트 ──


@router.post("/monitor/start", response_model=MessageResponse)
def start_monitor():
    """모니터링 시작."""
    service = _get_monitor_service()
    if service.is_running:
        return MessageResponse(success=True, message="모니터링이 이미 실행 중입니다.")
    service.start()
    return MessageResponse(success=True, message="모니터링이 시작되었습니다.")


@router.post("/monitor/stop", response_model=MessageResponse)
def stop_monitor():
    """모니터링 중지."""
    service = _get_monitor_service()
    if not service.is_running:
        return MessageResponse(success=True, message="모니터링이 실행 중이 아닙니다.")
    service.stop()
    return MessageResponse(success=True, message="모니터링이 중지되었습니다.")


@router.get("/monitor/status", response_model=MonitorStatusResponse)
def get_monitor_status():
    """모니터링 상태 조회."""
    service = _get_monitor_service()
    return MonitorStatusResponse(
        running=service.is_running,
        last_run=service.last_run.isoformat() if service.last_run else None,
    )
