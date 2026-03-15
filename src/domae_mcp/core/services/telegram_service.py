"""텔레그램 알림 서비스"""

import logging
from typing import Optional

import requests

logger = logging.getLogger(__name__)


class TelegramService:
    """텔레그램 봇 API를 통한 알림 전송.

    token 또는 chat_id가 없으면 비활성 상태로 동작하며,
    모든 전송 요청을 조용히 무시한다.
    """

    TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"

    def __init__(self, token: Optional[str] = None, chat_id: Optional[str] = None):
        self.token = token or ""
        self.chat_id = chat_id or ""

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.chat_id)

    def send_message(self, text: str) -> bool:
        """텔레그램 메시지 전송. 비활성이면 조용히 무시."""
        if not self.enabled:
            return False
        try:
            url = self.TELEGRAM_API.format(token=self.token)
            resp = requests.post(url, json={
                "chat_id": self.chat_id,
                "text": text,
                "parse_mode": "HTML",
            }, timeout=10)
            resp.raise_for_status()
            return True
        except Exception:
            logger.warning("텔레그램 전송 실패", exc_info=True)
            return False

    def send_price_alert(
        self,
        product_name: str,
        supplier: str,
        old_price: int,
        new_price: int,
    ) -> bool:
        """가격 변동 알림."""
        diff = new_price - old_price
        arrow = "▲" if diff > 0 else "▼"
        text = (
            f"<b>[가격변동]</b>\n"
            f"제품: {product_name}\n"
            f"도매: {supplier}\n"
            f"가격: {old_price:,}원 → {new_price:,}원 ({arrow}{abs(diff):,}원)"
        )
        return self.send_message(text)

    def send_stock_alert(
        self,
        product_name: str,
        supplier: str,
        quantity: int,
        price: int,
    ) -> bool:
        """재고 감지 알림."""
        text = (
            f"<b>[재고감지]</b>\n"
            f"제품: {product_name}\n"
            f"도매: {supplier}\n"
            f"수량: {quantity}개 / 가격: {price:,}원"
        )
        return self.send_message(text)

    def send_order_alert(
        self,
        product_name: str,
        supplier: str,
        quantity: int,
        success: bool,
        message: str,
    ) -> bool:
        """주문 결과 알림."""
        status = "성공" if success else "실패"
        text = (
            f"<b>[주문{status}]</b>\n"
            f"제품: {product_name}\n"
            f"도매: {supplier}\n"
            f"수량: {quantity}개\n"
            f"메시지: {message}"
        )
        return self.send_message(text)
