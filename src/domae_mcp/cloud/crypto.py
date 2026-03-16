"""크리덴셜 AES-256-GCM 암복호화

환경변수 DOMAE_CREDENTIAL_KEY에 base64 인코딩된 32바이트 키를 설정.
키 생성: python -c "import os,base64; print(base64.b64encode(os.urandom(32)).decode())"
"""

import base64
import json
import logging
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

logger = logging.getLogger(__name__)


def _get_key() -> bytes:
    """환경변수에서 AES-256 키 로드 (32바이트)."""
    key_b64 = os.environ.get("DOMAE_CREDENTIAL_KEY")
    if not key_b64:
        raise RuntimeError("DOMAE_CREDENTIAL_KEY 환경변수가 설정되지 않았습니다.")
    key = base64.b64decode(key_b64)
    if len(key) != 32:
        raise RuntimeError(f"DOMAE_CREDENTIAL_KEY는 32바이트여야 합니다 (현재: {len(key)})")
    return key


def encrypt_credentials(credentials: dict) -> str:
    """크리덴셜 dict → AES-256-GCM 암호화 → base64 문자열.

    저장 형식: base64(nonce(12) + ciphertext + tag(16))
    """
    key = _get_key()
    aesgcm = AESGCM(key)
    nonce = os.urandom(12)
    plaintext = json.dumps(credentials, ensure_ascii=False).encode("utf-8")
    ciphertext = aesgcm.encrypt(nonce, plaintext, None)
    return base64.b64encode(nonce + ciphertext).decode("ascii")


def decrypt_credentials(encrypted: str) -> dict:
    """base64 암호문 → AES-256-GCM 복호화 → 크리덴셜 dict."""
    key = _get_key()
    aesgcm = AESGCM(key)
    raw = base64.b64decode(encrypted)
    nonce = raw[:12]
    ciphertext = raw[12:]
    plaintext = aesgcm.decrypt(nonce, ciphertext, None)
    return json.loads(plaintext.decode("utf-8"))
