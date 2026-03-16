"""알림 발송 (텔레그램/카카오)"""
import logging

import requests

logger = logging.getLogger(__name__)


class Notifier:
    @staticmethod
    def send_telegram(token: str, chat_id: str, message: str) -> bool:
        if not token or not chat_id:
            return False
        try:
            resp = requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
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
