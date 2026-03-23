"""텔레그램 Webhook 콜백 핸들러.

텔레그램 인라인 버튼 클릭 → callback_query 수신 → Redis 주문 큐 push.
pharmsquare-server에서 프록시하거나, 이 모듈을 직접 FastAPI에 마운트.
"""
import json
import logging
import os

import redis
import requests

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org/bot{token}"

# Redis 싱글턴 (매 호출 연결 생성 방지)
_redis_client: redis.Redis | None = None


def _get_redis() -> redis.Redis:
    global _redis_client
    if _redis_client is None:
        _redis_client = redis.Redis.from_url(
            os.environ.get("REDIS_URL", "redis://localhost:6379"),
            decode_responses=True,
        )
    return _redis_client


def _get_token() -> str:
    return os.environ.get("DOMAE_TELEGRAM_BOT_TOKEN", "")


def handle_telegram_update(update: dict) -> dict:
    """텔레그램 Update 객체 처리.

    Args:
        update: 텔레그램 Webhook으로 수신된 Update JSON.

    Returns:
        {"ok": True/False, "message": ...}
    """
    callback_query = update.get("callback_query")
    if not callback_query:
        return {"ok": True, "message": "not a callback_query"}

    query_id = callback_query.get("id", "")
    data = callback_query.get("data", "")
    message = callback_query.get("message", {})
    chat_id = str(message.get("chat", {}).get("id", ""))
    message_id = message.get("message_id")
    original_text = message.get("text", "")

    # 콜백 데이터 파싱
    # C:{...} = 확인 요청 (서버에서 처리, 여기서는 무시 — domae.ts에서 직접 처리)
    # O:{...} = 주문 실행
    # X:cancel = 취소
    if data.startswith("C:") or data == "X:cancel":
        # 확인/취소는 domae.ts webhook에서 직접 처리하므로 여기서는 패스
        return {"ok": True, "message": "handled by webhook router"}

    if not data.startswith("O:"):
        _answer_callback(query_id, "알 수 없는 명령")
        return {"ok": False, "message": "unknown callback_data"}

    parts = data.split(":")
    if len(parts) != 5:
        _answer_callback(query_id, "잘못된 데이터")
        return {"ok": False, "message": f"invalid callback_data: {data}"}

    _, monitor_prefix, supplier, product_id, qty_str = parts

    try:
        quantity = int(qty_str)
    except ValueError:
        _answer_callback(query_id, "수량 오류")
        return {"ok": False, "message": f"invalid quantity: {qty_str}"}

    # 즉시 콜백 응답 (텔레그램 로딩 제거)
    _answer_callback(query_id, f"{quantity}개 주문 처리 중...")

    # 메시지 편집: 처리 중 상태 표시 + 버튼 제거
    token = _get_token()
    if token and message_id:
        try:
            requests.post(
                f"{TELEGRAM_API.format(token=token)}/editMessageText",
                json={
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "text": original_text + f"\n\n⏳ {quantity}개 주문 처리 중...",
                    "parse_mode": "HTML",
                },
                timeout=10,
            )
        except Exception as e:
            logger.warning("메시지 편집 실패: %s", e)

    # Redis 큐에 주문 job push
    job = {
        "action": "telegram_order",
        "monitor_prefix": monitor_prefix,
        "supplier": supplier,
        "product_id": product_id,
        "quantity": quantity,
        "chat_id": chat_id,
        "message_id": message_id,
        "original_text": original_text,
    }

    try:
        r = _get_redis()
        r.lpush("domae:jobs:urgent", json.dumps(job))
        logger.info("텔레그램 주문 job push: %s %s x%d", supplier, product_id, quantity)
        return {"ok": True, "message": "order queued"}
    except Exception as e:
        logger.error("Redis push 실패: %s", e)
        # 실패 시 메시지 편집
        if token and message_id:
            try:
                requests.post(
                    f"{TELEGRAM_API.format(token=token)}/editMessageText",
                    json={
                        "chat_id": chat_id,
                        "message_id": message_id,
                        "text": original_text + "\n\n❌ 주문 접수 실패 (서버 오류)",
                        "parse_mode": "HTML",
                    },
                    timeout=10,
                )
            except Exception:
                pass
        return {"ok": False, "message": str(e)}


def _answer_callback(query_id: str, text: str = ""):
    """텔레그램 콜백 응답."""
    token = _get_token()
    if not token:
        return
    try:
        requests.post(
            f"{TELEGRAM_API.format(token=token)}/answerCallbackQuery",
            json={"callback_query_id": query_id, "text": text},
            timeout=10,
        )
    except Exception as e:
        logger.warning("콜백 응답 실패: %s", e)
