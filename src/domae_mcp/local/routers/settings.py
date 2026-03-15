"""설정 라우터: 계정, 텔레그램, 스케줄 관리"""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from domae_mcp.core.crawlers import CrawlerRegistry
from domae_mcp.core.models import MonitorSchedule
from domae_mcp.local.config import ConfigManager
from domae_mcp.local.database import get_db

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


class ScheduleListResponse(BaseModel):
    schedules: list[ScheduleItem]


class ScheduleUpdateItem(BaseModel):
    id: Optional[int] = None
    start_hour: int
    end_hour: int
    interval_minutes: int


class ScheduleUpdateRequest(BaseModel):
    schedules: list[ScheduleUpdateItem]


class MessageResponse(BaseModel):
    success: bool
    message: str


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
        )
        db.add(schedule)

    db.commit()
    return MessageResponse(success=True, message="스케줄이 저장되었습니다.")
