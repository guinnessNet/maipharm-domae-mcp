"""도매 v2 서명 신뢰 목록 (스펙 B1 공통 1, §11, P2). 서버가 보낸 공개키·공개 GET 응답으로 갱신하지 않는다."""
import base64
import json

from .encoding import EncodingError, b64d
from .runtime import build_kind, dev_override

# 운영 서명 공개키(공개 값, B-3 H3 기록)
PRODUCTION_TRUST: dict[str, bytes] = {
    "ed25519-0477f26ca36a1ebd": base64.b64decode("YfK9csPzK1keTJix+7X2g8NntM3bj32kEiPekBRqITc="),
}


def _read_trust_file(path: str) -> dict[str, bytes]:
    """{key_id: base64 공개키} 또는 signing-key GET 응답 모양 {"signing_key_id", "public_key"}."""
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    if isinstance(raw, dict) and set(raw) == {"signing_key_id", "public_key"}:
        raw = {raw["signing_key_id"]: raw["public_key"]}
    if not isinstance(raw, dict) or not raw:
        raise ValueError("trust_file")
    out = {}
    for k, v in raw.items():
        if not isinstance(k, str) or not k:
            raise ValueError("trust_file")
        try:
            out[k] = b64d(v, 32)
        except EncodingError:
            raise ValueError("trust_file") from None
    return out


def active_trust() -> dict[str, bytes]:
    trust = dict(PRODUCTION_TRUST)
    if build_kind() == "release":
        return trust
    path = dev_override("DOMAE_V2_TRUST_FILE")
    if path:
        trust.update(_read_trust_file(path))
    return trust
