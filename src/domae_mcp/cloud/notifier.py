"""알림 발송 (텔레그램/카카오)"""
import html
import json
import logging
import os
import re
from typing import Optional

import requests

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org/bot{token}"


class Notifier:
    """텔레그램 알림 발송 — 인라인 버튼 및 메시지 편집 지원."""

    # ── 기본 전송 ──────────────────────────────────────

    @staticmethod
    def _get_token() -> str:
        return os.environ.get("DOMAE_TELEGRAM_BOT_TOKEN", "")

    @staticmethod
    def send_telegram(chat_id: str, message: str, reply_markup: Optional[dict] = None) -> Optional[int]:
        """텔레그램 메시지 전송.

        Returns:
            성공 시 message_id (int), 실패 시 None.
        """
        token = Notifier._get_token()
        if not token:
            logger.warning("DOMAE_TELEGRAM_BOT_TOKEN 환경변수 미설정")
            return None
        if not chat_id:
            logger.warning("텔레그램 chat_id 없음")
            return None
        try:
            payload: dict = {
                "chat_id": chat_id,
                "text": message,
                "parse_mode": "HTML",
            }
            if reply_markup:
                payload["reply_markup"] = reply_markup

            resp = requests.post(
                f"{TELEGRAM_API.format(token=token)}/sendMessage",
                json=payload,
                timeout=10,
            )
            if resp.status_code != 200:
                logger.warning("텔레그램 API 응답 오류: %d %s", resp.status_code, resp.text[:200])
                return None

            data = resp.json()
            return data.get("result", {}).get("message_id")
        except Exception as e:
            logger.warning("텔레그램 발송 실패: %s", e)
            return None

    @staticmethod
    def edit_message(chat_id: str, message_id: int, text: str, reply_markup: Optional[dict] = None) -> bool:
        """기존 메시지 편집 (버튼 제거/변경 포함)."""
        token = Notifier._get_token()
        if not token:
            return False
        try:
            payload: dict = {
                "chat_id": chat_id,
                "message_id": message_id,
                "text": text,
                "parse_mode": "HTML",
            }
            if reply_markup:
                payload["reply_markup"] = reply_markup

            resp = requests.post(
                f"{TELEGRAM_API.format(token=token)}/editMessageText",
                json=payload,
                timeout=10,
            )
            return resp.status_code == 200
        except Exception as e:
            logger.warning("텔레그램 메시지 편집 실패: %s", e)
            return False

    @staticmethod
    def answer_callback(callback_query_id: str, text: str = "") -> bool:
        """인라인 버튼 콜백 응답 (로딩 스피너 제거)."""
        token = Notifier._get_token()
        if not token:
            return False
        try:
            resp = requests.post(
                f"{TELEGRAM_API.format(token=token)}/answerCallbackQuery",
                json={"callback_query_id": callback_query_id, "text": text},
                timeout=10,
            )
            return resp.status_code == 200
        except Exception as e:
            logger.warning("콜백 응답 실패: %s", e)
            return False

    # ── 이벤트별 알림 메시지 ──────────────────────────

    @staticmethod
    def _sanitize_cb_field(value: str, max_len: int) -> str:
        """callback_data 필드에서 콜론 제거 + 길이 제한."""
        return re.sub(r":", "", value)[:max_len]

    @staticmethod
    def send_restock_alert(
        chat_id: str,
        monitor_id: str,
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

        # 콜백 데이터: C:{monitor_id_8자}:{supplier}:{product_id}:{qty}
        # C = 확인 요청 (1단계), 확인 후 O = 주문 실행 (2단계)
        # 텔레그램 callback_data 최대 64바이트
        mid = Notifier._sanitize_cb_field(monitor_id, 8)
        sup = Notifier._sanitize_cb_field(supplier, 10)
        pid = Notifier._sanitize_cb_field(product_id, 16)
        buttons = []
        for qty in (1, 3, 5):
            cb_data = f"C:{mid}:{sup}:{pid}:{qty}"
            if len(cb_data.encode("utf-8")) <= 64:
                buttons.append({"text": f"{qty}개 주문", "callback_data": cb_data})

        reply_markup = {"inline_keyboard": [buttons]} if buttons else None
        return Notifier.send_telegram(chat_id, text, reply_markup=reply_markup)

    @staticmethod
    def send_stock_drop_alert(
        chat_id: str,
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
        total_value = (price or 0) * (new_qty or 0)
        total_str = f"잔여 총금액 {total_value:,}원\n" if total_value else ""
        price_str = f"단가 {price:,}원" if price else ""
        text = (
            f"🔴 <b>재고 급감</b>\n"
            f"{total_str}"
            f"{safe_sup} {safe_name}\n"
            f"{old_qty}개 → {new_qty}개 (▼{pct}%)\n"
            f"{price_str}"
        ).strip()
        return Notifier.send_telegram(chat_id, text)

    @staticmethod
    def send_urgent_order_result(
        chat_id: str,
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
        return Notifier.send_telegram(chat_id, text)

    @staticmethod
    def send_order_result(
        chat_id: str,
        message_id: Optional[int],
        original_text: str,
        product_name: str,
        supplier: str,
        quantity: int,
        price: int,
        success: bool,
        error_msg: str = "",
    ) -> bool:
        """버튼 주문 결과 — 원본 메시지 편집 또는 새 메시지."""
        safe_sup = html.escape(supplier)
        safe_name = html.escape(product_name)
        safe_err = html.escape(error_msg or "알 수 없음")
        if success:
            total_price = price * quantity if price else 0
            total_str = f"\n주문금액 {total_price:,}원" if total_price else ""
            result_text = f"\n\n✅ <b>주문 완료</b>\n{safe_sup} {safe_name} × {quantity}개{total_str}"
        else:
            result_text = f"\n\n❌ <b>주문 실패</b>\n{safe_sup} {safe_name} × {quantity}개\n사유: {safe_err}"

        # 버튼 제거용 빈 reply_markup
        no_buttons = {"inline_keyboard": []}

        if message_id:
            # original_text는 콜백 메시지의 plain text → HTML 이스케이프 필수
            safe_original = html.escape(original_text)
            updated = safe_original + result_text
            ok = Notifier.edit_message(chat_id, message_id, updated, reply_markup=no_buttons)
            if not ok:
                # 편집 실패 시 새 메시지로 fallback
                logger.warning("메시지 편집 실패, 새 메시지로 발송 (chat=%s, msg=%s)", chat_id, message_id)
                Notifier.send_telegram(chat_id, result_text.strip())
            return True
        else:
            Notifier.send_telegram(chat_id, result_text.strip())
            return True

    # ── 카카오 (미구현) ──────────────────────────────

    @staticmethod
    def send_kakao(user_id: str, message: str) -> bool:
        # TODO: 카카오 알림톡 연동
        logger.debug("카카오 알림 미구현: %s", user_id)
        return False
