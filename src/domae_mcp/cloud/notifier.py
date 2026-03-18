"""알림 발송 (텔레그램/카카오)"""
import logging
import os

import requests

logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("DOMAE_TELEGRAM_BOT_TOKEN", "")


class Notifier:
    @staticmethod
    def send_telegram(chat_id: str, message: str) -> bool:
        token = os.environ.get("DOMAE_TELEGRAM_BOT_TOKEN", "")
        if not token:
            logger.warning("DOMAE_TELEGRAM_BOT_TOKEN 환경변수 미설정")
            return False
        if not chat_id:
            logger.warning("텔레그램 chat_id 없음")
            return False
        try:
            resp = requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": message, "parse_mode": "HTML"},
                timeout=10,
            )
            if resp.status_code != 200:
                logger.warning("텔레그램 API 응답 오류: %d %s", resp.status_code, resp.text[:200])
            return resp.status_code == 200
        except Exception as e:
            logger.warning("텔레그램 발송 실패: %s", e)
            return False

    @staticmethod
    def send_kakao(user_id: str, message: str) -> bool:
        # TODO: 카카오 알림톡 연동
        logger.debug("카카오 알림 미구현: %s", user_id)
        return False
