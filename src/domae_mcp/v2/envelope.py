"""도매 v2 서명 envelope (스펙 B1 서명 대상, §11 신뢰 목록). 서버가 보낸 공개키는 쓰지 않는다."""
import hashlib

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .encoding import EncodingError, b64d, lp

DOMAIN_BUNDLE = "domae-bundle-v2"
DOMAIN_RELEASE = "domae-release-v1"
DOMAIN_REVOCATION = "domae-revocation-v1"
DOMAIN_UPDATE = "domae-update-v1"
_DOMAINS = {DOMAIN_BUNDLE, DOMAIN_RELEASE, DOMAIN_REVOCATION, DOMAIN_UPDATE}


class EnvelopeError(ValueError):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def signed_digest(domain: str, signing_key_id: str, payload: str) -> bytes:
    return hashlib.sha256(lp(domain.encode("utf-8")) + lp(signing_key_id.encode("utf-8"))
                          + lp(payload.encode("utf-8"))).digest()


def verify_envelope(envelope: dict, domain: str, trust: dict[str, bytes]) -> str:
    if domain not in _DOMAINS:
        raise EnvelopeError("bad_domain")
    if not isinstance(envelope, dict):
        raise EnvelopeError("malformed")
    payload, key_id, sig = envelope.get("payload"), envelope.get("signing_key_id"), envelope.get("signature")
    if not isinstance(payload, str) or not isinstance(key_id, str) or not isinstance(sig, str):
        raise EnvelopeError("malformed")
    try:
        sig_bytes = b64d(sig, 64)
    except EncodingError:
        raise EnvelopeError("malformed") from None
    pub = trust.get(key_id)
    if pub is None:
        raise EnvelopeError("unknown_signing_key")
    try:
        digest = signed_digest(domain, key_id, payload)
    except UnicodeEncodeError:
        raise EnvelopeError("malformed") from None
    try:
        Ed25519PublicKey.from_public_bytes(pub).verify(sig_bytes, digest)
    except InvalidSignature:
        raise EnvelopeError("bad_signature") from None
    return payload
