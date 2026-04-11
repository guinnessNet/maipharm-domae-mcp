"""크리덴셜 + 크롤러 코드 AES-256-GCM 암복호화

크리덴셜 키: DOMAE_CREDENTIAL_KEY (base64 32바이트)
크롤러 키: DOMAE_CRAWLER_KEY (없으면 DOMAE_CREDENTIAL_KEY로 fallback)
키 생성: python -c "import os,base64; print(base64.b64encode(os.urandom(32)).decode())"
"""

import base64
import json
import logging
import os

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


def _get_crawler_key() -> bytes:
    """크롤러 코드 암복호화용 AES-256 키 로드. DOMAE_CRAWLER_KEY 우선, 없으면 크리덴셜 키 재사용."""
    key_b64 = os.environ.get("DOMAE_CRAWLER_KEY") or os.environ.get("DOMAE_CREDENTIAL_KEY")
    if not key_b64:
        raise RuntimeError("DOMAE_CRAWLER_KEY 또는 DOMAE_CREDENTIAL_KEY 환경변수가 설정되지 않았습니다.")
    key = base64.b64decode(key_b64)
    if len(key) != 32:
        raise RuntimeError(f"크롤러 키는 32바이트여야 합니다 (현재: {len(key)})")
    return key


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
    'v1:' prefix 없으면 legacy 평문으로 간주하여 그대로 반환 (마이그레이션 안전).
    """
    if not stored.startswith(CRAWLER_MAGIC_PREFIX):
        return stored  # legacy 평문
    key = _get_crawler_key()
    aesgcm = AESGCM(key)
    raw = base64.b64decode(stored[len(CRAWLER_MAGIC_PREFIX):])
    if len(raw) < 12 + 16:
        raise RuntimeError("크롤러 암호문 길이가 부족합니다.")
    nonce = raw[:12]
    ciphertext = raw[12:]
    plaintext = aesgcm.decrypt(nonce, ciphertext, None)
    return plaintext.decode("utf-8")
