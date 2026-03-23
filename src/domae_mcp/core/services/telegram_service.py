"""텔레그램 알림 서비스 — 인라인 버튼 + 이벤트별 개별 알림"""

import html
import logging
from typing import Optional

import requests

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org/bot{token}"


class TelegramService:
    """텔레그램 봇 API를 통한 알림 전송.

    token 또는 chat_id가 없으면 비활성 상태로 동작하며,
    모든 전송 요청을 조용히 무시한다.
    """

    def __init__(self, token: Optional[str] = None, chat_id: Optional[str] = None):
        self.token = token or ""
        self.chat_id = chat_id or ""

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.chat_id)

    # ── 기본 전송 ──────────────────────────────────────

    def send_message(self, text: str, reply_markup: Optional[dict] = None) -> Optional[int]:
        """텔레그램 메시지 전송. 비활성이면 조용히 무시.

        Returns:
            성공 시 message_id (int), 실패 시 None.
        """
        if not self.enabled:
            return None
        try:
            payload: dict = {
                "chat_id": self.chat_id,
                "text": text,
                "parse_mode": "HTML",
            }
            if reply_markup:
                payload["reply_markup"] = reply_markup

            resp = requests.post(
                f"{TELEGRAM_API.format(token=self.token)}/sendMessage",
                json=payload,
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            return data.get("result", {}).get("message_id")
        except Exception:
            logger.warning("텔레그램 전송 실패", exc_info=True)
            return None

    def edit_message(self, message_id: int, text: str, reply_markup: Optional[dict] = None) -> bool:
        """기존 메시지 편집."""
        if not self.enabled:
            return False
        try:
            payload: dict = {
                "chat_id": self.chat_id,
                "message_id": message_id,
                "text": text,
                "parse_mode": "HTML",
            }
            if reply_markup:
                payload["reply_markup"] = reply_markup

            resp = requests.post(
                f"{TELEGRAM_API.format(token=self.token)}/editMessageText",
                json=payload,
                timeout=10,
            )
            return resp.status_code == 200
        except Exception:
            logger.warning("텔레그램 메시지 편집 실패", exc_info=True)
            return False

    # ── 이벤트별 알림 ──────────────────────────────────

    def send_restock_alert(
        self,
        supplier: str,
        product_name: str,
        product_id: str,
        quantity: int,
        price: int,
    ) -> Optional[int]:
        """재입고 알림 — 인라인 주문 버튼 포함."""
        safe_sup = html.escape(supplier)
        safe_name = html.escape(product_name)
        price_str = f"단가 {price:,}원" if price else "가격 미확인"
        text = (
            f"🟢 <b>입고</b>\n"
            f"{safe_sup} {safe_name}\n"
            f"{quantity}개 입고 | {price_str}"
        )

        # 로컬 모드에서는 버튼 없이 전송 (webhook 없으므로)
        return self.send_message(text)

    def send_stock_drop_alert(
        self,
        supplier: str,
        product_name: str,
        old_qty: int,
        new_qty: int,
        price: int,
    ) -> Optional[int]:
        """급격한 재고 감소 알림 (30% 이상)."""
        safe_sup = html.escape(supplier)
        safe_name = html.escape(product_name)
        pct = round((1 - new_qty / old_qty) * 100) if old_qty > 0 else 100
        price_str = f"단가 {price:,}원" if price else ""
        text = (
            f"🔴 <b>재고 급감</b>\n"
            f"{safe_sup} {safe_name}\n"
            f"{old_qty}개 → {new_qty}개 (▼{pct}%)\n"
            f"{price_str}"
        ).strip()
        return self.send_message(text)

    def send_urgent_order_result(
        self,
        product_name: str,
        supplier: str,
        quantity: int,
        price: int,
        filled: int,
        total: int,
    ) -> Optional[int]:
        """긴급주문 자동 체결 알림."""
        safe_sup = html.escape(supplier)
        safe_name = html.escape(product_name)
        price_str = f" | 단가 {price:,}원" if price else ""
        remaining = total - filled
        text = (
            f"⚡ <b>긴급주문 체결</b>\n"
            f"{safe_sup} {safe_name}\n"
            f"{quantity}개 주문 완료{price_str}\n"
            f"목표 {total}개 중 {filled}개 확보 (나머지 {remaining}개)"
        )
        return self.send_message(text)

    # ── 레거시 호환 (기존 호출부가 있을 수 있으므로 유지) ──

    def send_price_alert(self, product_name: str, supplier: str, old_price: int, new_price: int) -> bool:
        """가격 변동 알림 (레거시 — 더 이상 모니터링에서 호출하지 않음)."""
        diff = new_price - old_price
        arrow = "▲" if diff > 0 else "▼"
        text = (
            f"<b>[가격변동]</b>\n"
            f"제품: {product_name}\n"
            f"도매: {supplier}\n"
            f"가격: {old_price:,}원 → {new_price:,}원 ({arrow}{abs(diff):,}원)"
        )
        return self.send_message(text) is not None

    def send_stock_alert(self, product_name: str, supplier: str, quantity: int, price: int) -> bool:
        """재고 감지 알림 (레거시 — 더 이상 모니터링에서 호출하지 않음)."""
        text = (
            f"<b>[재고감지]</b>\n"
            f"제품: {product_name}\n"
            f"도매: {supplier}\n"
            f"수량: {quantity}개 / 가격: {price:,}원"
        )
        return self.send_message(text) is not None

    def send_order_alert(self, product_name: str, supplier: str, quantity: int, success: bool, message: str) -> bool:
        """주문 결과 알림 (레거시 — 더 이상 모니터링에서 호출하지 않음)."""
        status = "성공" if success else "실패"
        text = (
            f"<b>[주문{status}]</b>\n"
            f"제품: {product_name}\n"
            f"도매: {supplier}\n"
            f"수량: {quantity}개\n"
            f"메시지: {message}"
        )
        return self.send_message(text) is not None
