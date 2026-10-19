"""설정 라우터: API 키, 계정, 텔레그램, 스케줄 관리"""

import asyncio
import logging
from typing import Optional

import requests
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from domae_mcp.core.crawlers import CrawlerRegistry
from domae_mcp.core.crawlers.registry import CrawlerBlocked
from domae_mcp.core.models import MonitorSchedule
from domae_mcp.local import v2app
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
    crawler_state: dict
    protected: bool
    reentry_required: list[str]


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
    """초기 설정 상태 조회 (셋업 위자드용). 키 원문은 내보내지 않는다."""
    gate = v2app.get_gate()
    state = gate.status()
    creds = _config.get_all_credentials()
    tg = _config.get_telegram()
    configured = sum(1 for c in creds if c["configured"])
    key_set = state["state"] != "no_key"

    return SetupStatusResponse(
        api_key_set=key_set,
        api_key_prefix=v2app.MASKED_KEY if key_set else "",
        credentials_configured=configured,
        credentials_total=len(creds),
        telegram_set=bool(tg.get("token") and tg.get("chat_id")),
        crawler_count=state["crawler_count"],
        crawler_state=state,
        protected=state["protected"],
        reentry_required=_config.reentry_required() if hasattr(_config, "reentry_required") else [],
    )


@router.post("/settings/api-key/verify", response_model=ApiKeyVerifyResponse)
async def verify_api_key(req: ApiKeySaveRequest):
    """API 키 검증 = 도매 v2 초기화(버전 확인 → 번들 다운로드 → 전체 로드 성공 뒤 키 저장).
    실패하면 활성 키·캐시·레지스트리는 그대로다."""
    valid, message, count = await asyncio.to_thread(v2app.verify_key, req.api_key)
    return ApiKeyVerifyResponse(
        valid=valid,
        tier="local" if valid else "",
        pharmacy_name="",
        message=message,
        crawler_count=count,
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

    from domae_mcp.core.crawlers.base import CrawlerError

    CrawlerRegistry.require()  # 막히면 423
    try:
        crawler = CrawlerRegistry.get(req.supplier)
        crawler.ensure_login(cred["login_id"], cred["login_pw"])
        return MessageResponse(success=True, message="로그인 성공")
    except CrawlerBlocked:
        raise
    except CrawlerError as e:
        logger.warning("로그인 실패 (%s): %s", req.supplier, e)
        return MessageResponse(success=False, message="로그인 실패: ID/비밀번호를 확인해주세요")
    except Exception as e:
        logger.error("로그인 예외 (%s): %s", req.supplier, e, exc_info=True)
        return MessageResponse(success=False, message="로그인 중 오류가 발생했습니다")


# ── 텔레그램 엔드포인트 ──


@router.get("/settings/telegram", response_model=TelegramSettings)
def get_telegram():
    """텔레그램 설정 조회."""
    tg = _config.get_telegram()
    return TelegramSettings(**tg)


@router.put("/settings/telegram", response_model=MessageResponse)
def save_telegram(req: TelegramSettings):
    """텔레그램 설정 저장. 실행 중인 모니터링에 즉시 반영."""
    _config.set_telegram(req.token, req.chat_id)

    # 실행 중인 모니터링 서비스에 즉시 반영
    from domae_mcp.local.routers.monitor import _monitor_service
    if _monitor_service and _monitor_service.is_running:
        from domae_mcp.core.services.telegram_service import TelegramService
        _monitor_service.update_telegram(TelegramService(token=req.token, chat_id=req.chat_id))
        logger.info("실행 중인 모니터링에 텔레그램 설정 반영됨")

    return MessageResponse(success=True, message="텔레그램 설정이 저장되었습니다.")


@router.post("/settings/telegram/test", response_model=MessageResponse)
def test_telegram():
    """현재 저장된 설정으로 테스트 메시지 발송."""
    from domae_mcp.core.services.telegram_service import TelegramService

    tg = _config.get_telegram()
    token = tg.get("token", "")
    chat_id = tg.get("chat_id", "")
    if not token or not chat_id:
        return MessageResponse(success=False, message="봇 토큰과 Chat ID를 먼저 설정해주세요.")

    svc = TelegramService(token=token, chat_id=chat_id)
    result = svc.send_message("✅ 도매 모니터링 알림 테스트 메시지입니다.")
    if result is not None:
        return MessageResponse(success=True, message="테스트 메시지를 발송했습니다. 텔레그램을 확인해주세요.")
    return MessageResponse(success=False, message="발송 실패. 봇 토큰과 Chat ID를 다시 확인해주세요.")


class ChatIdCandidate(BaseModel):
    chat_id: str
    title: str
    chat_type: str


class FetchChatIdResponse(BaseModel):
    success: bool
    message: str
    candidates: list[ChatIdCandidate] = []


@router.post("/settings/telegram/fetch-chat-id", response_model=FetchChatIdResponse)
def fetch_chat_id(req: TelegramSettings):
    """봇 토큰으로 getUpdates를 호출하여 사용 가능한 Chat ID 목록을 반환."""
    token = req.token
    if not token:
        return FetchChatIdResponse(success=False, message="봇 토큰을 입력해주세요.")

    try:
        resp = requests.get(
            f"https://api.telegram.org/bot{token}/getUpdates",
            timeout=10,
        )
        data = resp.json()
        if not data.get("ok"):
            return FetchChatIdResponse(success=False, message="봇 토큰이 유효하지 않습니다.")

        seen: dict[str, ChatIdCandidate] = {}
        for update in data.get("result", []):
            msg = update.get("message") or update.get("my_chat_member", {}).get("chat")
            if not msg:
                continue
            chat = msg.get("chat") or msg
            cid = str(chat.get("id", ""))
            if not cid or cid in seen:
                continue
            chat_type = chat.get("type", "private")
            if chat_type == "private":
                title = chat.get("first_name", "") + " " + chat.get("last_name", "")
                title = title.strip() or "개인 채팅"
            else:
                title = chat.get("title", "그룹")
            seen[cid] = ChatIdCandidate(chat_id=cid, title=title, chat_type=chat_type)

        candidates = list(seen.values())
        if not candidates:
            return FetchChatIdResponse(
                success=False,
                message="봇에게 메시지를 먼저 보내주세요. (봇 검색 → 아무 메시지 전송)",
            )

        return FetchChatIdResponse(success=True, message=f"{len(candidates)}개 채팅 발견", candidates=candidates)
    except Exception as e:
        logger.error("Chat ID 조회 실패: %s", e, exc_info=True)
        return FetchChatIdResponse(success=False, message="조회 중 오류가 발생했습니다.")


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
