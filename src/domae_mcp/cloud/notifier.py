"""알림 발송 (텔레그램/카카오)"""
import logging
import os

import requests

logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("DOMAE_TELEGRAM_BOT_TOKEN", "")


class Notifier:
    @staticmethod
    def send_telegram(chat_id: str, message: str) -> bool:
        if not BOT_TOKEN or not chat_id:
            return False
        try:
            resp = requests.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                json={"chat_id": chat_id, "text": message, "parse_mode": "HTML"},
                timeout=10,
            )
            return resp.status_code == 200
        except Exception as e:
            logger.warning("텔레그램 발송 실패: %s", e)
            return False

    @staticmethod
    def send_kakao(user_id: str, message: str) -> bool:
        # TODO: 카카오 알림톡 연동
        logger.debug("카카오 알림 미구현: %s", user_id)
        return False
