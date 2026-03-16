"""설정 라우터: API 키, 계정, 텔레그램, 스케줄 관리"""

import asyncio
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from domae_mcp.core.crawlers import CrawlerRegistry
from domae_mcp.core.models import MonitorSchedule
from domae_mcp.local.config import ConfigManager
from domae_mcp.local.database import get_db

logger = logging.getLogger(__name__)

router = APIRouter(tags=["설정"])

_config = ConfigManager()


# ── Request/Response 스키마 ──


class CredentialItem(BaseModel):
    supplier: str
    login_id: str
    login_pw: str
    configured: bool


class CredentialsResponse(BaseModel):
    credentials: list[CredentialItem]


class SaveCredentialRequest(BaseModel):
    supplier: str
    login_id: str
    login_pw: str


class TestCredentialRequest(BaseModel):
    supplier: str


class TelegramSettings(BaseModel):
    token: str
    chat_id: str


class ScheduleItem(BaseModel):
    id: int
    start_hour: int
    end_hour: int
    interval_minutes: int
    enabled: bool = True


class ScheduleListResponse(BaseModel):
    schedules: list[ScheduleItem]


class ScheduleUpdateItem(BaseModel):
    id: Optional[int] = None
    start_hour: int
    end_hour: int
    interval_minutes: int
    enabled: bool = True


class ScheduleUpdateRequest(BaseModel):
    schedules: list[ScheduleUpdateItem]


class MessageResponse(BaseModel):
    success: bool
    message: str


# ── 셋업 상태 / API 키 엔드포인트 ──


class SetupStatusResponse(BaseModel):
    api_key_set: bool
    api_key_prefix: str
    credentials_configured: int
    credentials_total: int
    telegram_set: bool
    crawler_count: int


class ApiKeySaveRequest(BaseModel):
    api_key: str


class ApiKeyVerifyResponse(BaseModel):
    valid: bool
    tier: str
    pharmacy_name: str
    message: str
    crawler_count: int


@router.get("/settings/setup-status", response_model=SetupStatusResponse)
def get_setup_status():
    """초기 설정 상태 조회 (셋업 위자드용)."""
    api_key = _config.get_api_key()
    creds = _config.get_all_credentials()
    tg = _config.get_telegram()
    configured = sum(1 for c in creds if c["configured"])

    return SetupStatusResponse(
        api_key_set=bool(api_key),
        api_key_prefix=api_key[:16] + "..." if api_key and len(api_key) > 16 else (api_key or ""),
        credentials_configured=configured,
        credentials_total=len(creds),
        telegram_set=bool(tg.get("token") and tg.get("chat_id")),
        crawler_count=len(CrawlerRegistry.get_all()) if CrawlerRegistry.is_loaded() else 0,
    )


@router.put("/settings/api-key", response_model=MessageResponse)
def save_api_key(req: ApiKeySaveRequest):
    """API 키 저장. 기존 캐시 무효화 후 크롤러 재로드."""
    _config.set_api_key(req.api_key)

    # 캐시 무효화 (bundle.json 삭제)
    bundle_path = _config.base_dir / "crawlers" / "bundle.json"
    if bundle_path.exists():
        bundle_path.unlink()
        logger.info("크롤러 캐시 무효화됨")

    return MessageResponse(success=True, message="API 키가 저장되었습니다.")


@router.post("/settings/api-key/verify", response_model=ApiKeyVerifyResponse)
async def verify_api_key(req: ApiKeySaveRequest, request: Request):
    """API 키 검증 + 크롤러 다운로드."""
    from domae_mcp.local.api_key import ApiKeyManager

    api_key = req.api_key
    if not api_key.startswith("dmk_"):
        return ApiKeyVerifyResponse(
            valid=False, tier="", pharmacy_name="",
            message="API 키 형식이 올바르지 않습니다. (dmk_ 로 시작해야 합니다)",
            crawler_count=0,
        )

    # 1. 서버에서 검증
    manager = ApiKeyManager(_config)
    try:
        result = await manager.verify(api_key)
    except ValueError as e:
        return ApiKeyVerifyResponse(
            valid=False, tier="", pharmacy_name="",
            message=str(e), crawler_count=0,
        )

    # 2. API 키 저장
    _config.set_api_key(api_key)

    # 3. 크롤러 다운로드 + 레지스트리 갱신
    crawler_count = 0
    try:
        from domae_mcp.core.crawlers.loader import CrawlerLoader
        loader = CrawlerLoader(_config.base_dir, api_key)
        crawlers = await asyncio.to_thread(loader.load)
        CrawlerRegistry.register_all(crawlers)
        crawler_count = len(crawlers)
        # app.state에도 반영
        if hasattr(request.app.state, "crawler_loader"):
            request.app.state.crawler_loader = loader
    except Exception as e:
        logger.warning("API 키 검증 성공이나 크롤러 로드 실패: %s", e)

    return ApiKeyVerifyResponse(
        valid=True,
        tier=result.get("tier", "free"),
        pharmacy_name=result.get("pharmacy_name", ""),
        message=f"인증 완료! 크롤러 {crawler_count}개 로드됨.",
        crawler_count=crawler_count,
    )


# ── 계정 엔드포인트 ──


@router.get("/settings/credentials", response_model=CredentialsResponse)
def get_credentials():
    """도매별 계정 목록 조회 (비밀번호 마스킹)."""
    creds = _config.get_all_credentials()
    return CredentialsResponse(
        credentials=[CredentialItem(**c) for c in creds]
    )


@router.put("/settings/credentials", response_model=MessageResponse)
def save_credentials(req: SaveCredentialRequest):
    """도매 계정 저장."""
    _config.set_credentials(req.supplier, req.login_id, req.login_pw)
    return MessageResponse(success=True, message="계정이 저장되었습니다.")


@router.post("/settings/credentials/test", response_model=MessageResponse)
def test_credentials(req: TestCredentialRequest):
    """계정 연결 테스트 (실제 로그인 시도)."""
    cred = _config.get_credentials(req.supplier)
    if not cred.get("login_id") or not cred.get("login_pw"):
        raise HTTPException(
            status_code=400,
            detail=f"{req.supplier} 계정이 설정되지 않았습니다.",
        )

    try:
        crawler = CrawlerRegistry.get(req.supplier)
        crawler.ensure_login(cred["login_id"], cred["login_pw"])
        return MessageResponse(success=True, message="로그인 성공")
    except Exception as e:
        return MessageResponse(success=False, message=f"로그인 실패: {str(e)}")


# ── 텔레그램 엔드포인트 ──


@router.get("/settings/telegram", response_model=TelegramSettings)
def get_telegram():
    """텔레그램 설정 조회."""
    tg = _config.get_telegram()
    return TelegramSettings(**tg)


@router.put("/settings/telegram", response_model=MessageResponse)
def save_telegram(req: TelegramSettings):
    """텔레그램 설정 저장."""
    _config.set_telegram(req.token, req.chat_id)
    return MessageResponse(success=True, message="텔레그램 설정이 저장되었습니다.")


# ── 스케줄 엔드포인트 ──


@router.get("/settings/schedules", response_model=ScheduleListResponse)
def get_schedules(db: Session = Depends(get_db)):
    """모니터링 스케줄 조회."""
    schedules = db.query(MonitorSchedule).order_by(MonitorSchedule.start_hour).all()
    return ScheduleListResponse(
        schedules=[
            ScheduleItem(
                id=s.id,
                start_hour=s.start_hour,
                end_hour=s.end_hour,
                interval_minutes=s.interval_minutes,
                enabled=s.enabled,
            )
            for s in schedules
        ]
    )


@router.put("/settings/schedules", response_model=MessageResponse)
def update_schedules(req: ScheduleUpdateRequest, db: Session = Depends(get_db)):
    """모니터링 스케줄 수정 (전체 교체)."""
    # 기존 스케줄 삭제
    db.query(MonitorSchedule).delete()

    # 새 스케줄 추가
    for item in req.schedules:
        schedule = MonitorSchedule(
            start_hour=item.start_hour,
            end_hour=item.end_hour,
            interval_minutes=item.interval_minutes,
            enabled=item.enabled,
        )
        db.add(schedule)

    db.commit()
    return MessageResponse(success=True, message="스케줄이 저장되었습니다.")
