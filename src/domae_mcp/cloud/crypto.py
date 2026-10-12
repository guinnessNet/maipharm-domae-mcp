"""크리덴셜 + 크롤러 코드 AES-256-GCM 암복호화

크리덴셜 키: DOMAE_CREDENTIAL_KEY (base64 32바이트)
크롤러 키: DOMAE_CRAWLER_KEY (base64 32바이트, fallback 없음)
크롤러 이전 키: DOMAE_CRAWLER_KEY_PREVIOUS (키 교체 기간에만 두는 복호화 전용 키. 암호화에 쓰지 않는다)
키 생성: python -c "import os,base64; print(base64.b64encode(os.urandom(32)).decode())"
"""

import base64
import json
import logging
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

logger = logging.getLogger(__name__)

CRAWLER_MAGIC_PREFIX = "v1:"


def _get_credential_key() -> bytes:
    """크리덴셜 암복호화용 AES-256 키 로드."""
    key_b64 = os.environ.get("DOMAE_CREDENTIAL_KEY")
    if not key_b64:
        raise RuntimeError("DOMAE_CREDENTIAL_KEY 환경변수가 설정되지 않았습니다.")
    key = base64.b64decode(key_b64)
    if len(key) != 32:
        raise RuntimeError(f"DOMAE_CREDENTIAL_KEY는 32바이트여야 합니다 (현재: {len(key)})")
    return key


def _decode_crawler_key(name: str, key_b64: str) -> bytes:
    key = base64.b64decode(key_b64)
    if len(key) != 32:
        raise RuntimeError(f"{name} 는 32바이트여야 합니다 (현재: {len(key)})")
    return key


def _get_crawler_key() -> bytes:
    """크롤러 코드 암복호화용 AES-256 키 로드. DOMAE_CRAWLER_KEY 만 사용한다."""
    key_b64 = os.environ.get("DOMAE_CRAWLER_KEY")
    if not key_b64:
        raise RuntimeError("DOMAE_CRAWLER_KEY 환경변수가 설정되지 않았습니다.")
    return _decode_crawler_key("DOMAE_CRAWLER_KEY", key_b64)


def _get_previous_crawler_key() -> bytes | None:
    """키 교체 기간의 복호화 전용 이전 키. 없으면 None."""
    key_b64 = os.environ.get("DOMAE_CRAWLER_KEY_PREVIOUS")
    if not key_b64:
        return None
    return _decode_crawler_key("DOMAE_CRAWLER_KEY_PREVIOUS", key_b64)


def encrypt_credentials(credentials: dict) -> str:
    """크리덴셜 dict → AES-256-GCM 암호화 → base64 문자열.

    저장 형식: base64(nonce(12) + ciphertext + tag(16))
    """
    key = _get_credential_key()
    aesgcm = AESGCM(key)
    nonce = os.urandom(12)
    plaintext = json.dumps(credentials, ensure_ascii=False).encode("utf-8")
    ciphertext = aesgcm.encrypt(nonce, plaintext, None)
    return base64.b64encode(nonce + ciphertext).decode("ascii")


def decrypt_credentials(encrypted: str) -> dict:
    """base64 암호문 → AES-256-GCM 복호화 → 크리덴셜 dict."""
    key = _get_credential_key()
    aesgcm = AESGCM(key)
    raw = base64.b64decode(encrypted)
    nonce = raw[:12]
    ciphertext = raw[12:]
    plaintext = aesgcm.decrypt(nonce, ciphertext, None)
    return json.loads(plaintext.decode("utf-8"))


def encrypt_crawler_code(plaintext: str) -> str:
    """크롤러 Python 소스 → AES-256-GCM 암호화 → "v1:" + base64."""
    key = _get_crawler_key()
    aesgcm = AESGCM(key)
    nonce = os.urandom(12)
    ciphertext = aesgcm.encrypt(nonce, plaintext.encode("utf-8"), None)
    return CRAWLER_MAGIC_PREFIX + base64.b64encode(nonce + ciphertext).decode("ascii")


def decrypt_crawler_code(stored: str) -> str:
    """암호화된 크롤러 코드를 복호화.
    'v1:' 접두어가 없으면 거부한다.
    """
    if not stored.startswith(CRAWLER_MAGIC_PREFIX):
        raise RuntimeError("legacy plaintext crawler code is not allowed")
    key = _get_crawler_key()
    previous = _get_previous_crawler_key()
    raw = base64.b64decode(stored[len(CRAWLER_MAGIC_PREFIX):])
    if len(raw) < 12 + 16:
        raise RuntimeError("크롤러 암호문 길이가 부족합니다.")
    nonce = raw[:12]
    ciphertext = raw[12:]
    try:
        plaintext = AESGCM(key).decrypt(nonce, ciphertext, None)
    except InvalidTag:
        if previous is None or previous == key:
            raise
        # 키 교체 기간: 아직 재암호화하지 않은 행은 이전 키로 연다(암호화는 항상 현재 키)
        plaintext = AESGCM(previous).decrypt(nonce, ciphertext, None)
    return plaintext.decode("utf-8")
