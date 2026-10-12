"""도매 v2 클라이언트 검증 규칙 (스펙 B1 클라이언트 검증·v2 버전 확인, B5 철회 응답). 순수 함수.

검증과 집행을 나눈다. 호출자(B-4 로더)는 반드시 이 순서를 따른다.
1. verify_bundle_payload / verify_release_payload 로 신뢰·요청 바인딩·시간·세대·release_seq 역행을 검증한다.
   여기서 거부된 응답의 값(정책·세대)은 저장하지 않는다.
2. 돌려받은 VerifiedBundle.policy / VerifiedRelease.policy(저장값과 합친 최소 정책)를 보호 저장소에 저장한다.
   device_generation 은 저장값보다 클 때만 올린다(단조 증가). API 키가 바뀌면 이전 세대 기록을 쓰지 않는다.
3. check_loaded(저장한 정책, 실행할 release_seq) 로 실행 자격을 집행한다. 실패하면 캐시와 이미 로드한 실행 참조를
   모두 쓰지 않는다(protocol_too_old 는 업데이트 안내). 재시작·통신 실패 뒤에도 저장된 정책으로 같은 검사를 한다.
"""
import json
from dataclasses import dataclass

from .bundle_crypto import PROTOCOL_VERSION
from .encoding import (EncodingError, b64d, encode_runtime_id, hexd, validate_bundle_hash, validate_epoch,
                       validate_protocol_version, validate_release_seq, validate_safe_int)

MAX_LIFETIME_S = 144 * 3600
FRESHNESS_S = 600
RESPONSE_VALIDITY_S = 300


class PayloadRejected(ValueError):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class ClientIdentity:
    key_fp: bytes
    device_id_hex: str
    runtime_id: dict


@dataclass(frozen=True)
class PendingRequest:
    request_id_b64: str
    client_pub_b64: str


@dataclass(frozen=True)
class MinPolicy:
    min_release_seq: int
    min_protocol_version: int


@dataclass(frozen=True)
class VerifiedBundle:
    policy: MinPolicy
    device_generation: int


@dataclass(frozen=True)
class VerifiedRelease:
    policy: MinPolicy
    release_seq: int
    bundle_hash: str
    device_generation: int | None


def _no_dup(pairs):
    out = {}
    for k, v in pairs:
        if k in out:
            raise PayloadRejected("malformed")
        out[k] = v
    return out


def parse_payload(payload_str: str) -> dict:
    try:
        p = json.loads(payload_str, object_pairs_hook=_no_dup)
    except PayloadRejected:
        raise
    except (ValueError, TypeError):
        raise PayloadRejected("malformed") from None
    if not isinstance(p, dict):
        raise PayloadRejected("malformed")
    return p


def _v(fn, value, *args):
    try:
        return fn(value, *args)
    except EncodingError:
        raise PayloadRejected("malformed") from None


def _policy_of(d: dict) -> MinPolicy:
    if not isinstance(d, dict):
        raise PayloadRejected("malformed")
    return MinPolicy(_v(validate_release_seq, d.get("min_release_seq")),
                     _v(validate_protocol_version, d.get("min_protocol_version")))


def merge_min_policy(stored: MinPolicy, new: MinPolicy) -> MinPolicy:
    return MinPolicy(max(stored.min_release_seq, new.min_release_seq),
                     max(stored.min_protocol_version, new.min_protocol_version))


def check_loaded(policy: MinPolicy, loaded_release_seq: int) -> None:
    """저장된 최소 정책으로 실행 자격을 집행한다(검증 뒤 저장한 다음 호출). 실패하면 캐시·기존 실행 참조를 쓰지 않는다."""
    if PROTOCOL_VERSION < policy.min_protocol_version:
        raise PayloadRejected("protocol_too_old")
    if loaded_release_seq < policy.min_release_seq:
        raise PayloadRejected("downgrade")


def _generation(v, required: bool, stored: int | None) -> int | None:
    if v is None:
        if required:
            raise PayloadRejected("malformed")
        return None
    gen = _v(validate_safe_int, v, 0, "device_generation")
    if stored is not None and gen < stored:
        raise PayloadRejected("old_generation")
    return gen


def _identity(p: dict, ident: ClientIdentity, with_runtime: bool) -> None:
    pv = _v(validate_protocol_version, p.get("protocol_version"))
    fp = _v(b64d, p.get("key_fp"), 32)
    _v(hexd, p.get("device_id"), 32)
    if pv != PROTOCOL_VERSION or fp != ident.key_fp or p.get("device_id") != ident.device_id_hex:
        raise PayloadRejected("identity")
    if with_runtime and _v(encode_runtime_id, p.get("runtime_id")) != encode_runtime_id(ident.runtime_id):
        raise PayloadRejected("identity")


def _short_lived(p: dict, now: int) -> None:
    issued, valid_until = _v(validate_epoch, p.get("issued_at")), _v(validate_epoch, p.get("valid_until"))
    if valid_until != issued + RESPONSE_VALIDITY_S:
        raise PayloadRejected("malformed")
    if abs(now - issued) > FRESHNESS_S or now >= valid_until:
        raise PayloadRejected("stale")


def verify_bundle_payload(p: dict, ident: ClientIdentity, mode: str, now: int, highest_release_seq: int,
                          stored_policy: MinPolicy, stored_generation: int | None,
                          pending: PendingRequest | None = None) -> VerifiedBundle:
    if mode not in ("network", "cache") or (mode == "network") != (pending is not None):
        raise PayloadRejected("mode")
    issued = _v(validate_epoch, p.get("issued_at"))
    expires = _v(validate_epoch, p.get("expires_at"))
    deadline = _v(validate_epoch, p.get("offline_deadline"))
    seq = _v(validate_release_seq, p.get("release_seq"))
    _v(validate_bundle_hash, p.get("bundle_hash"))
    manifest = p.get("manifest")
    policy = merge_min_policy(stored_policy, _policy_of(manifest))
    _identity(p, ident, with_runtime=True)
    gen = _generation(p.get("device_generation"), required=True, stored=stored_generation)
    if not (issued <= expires <= deadline) or deadline - issued > MAX_LIFETIME_S:
        raise PayloadRejected("lifetime")
    if seq < highest_release_seq:
        raise PayloadRejected("downgrade")
    if mode == "network":
        if p.get("request_id") != pending.request_id_b64 or p.get("client_pub") != pending.client_pub_b64:
            raise PayloadRejected("request_mismatch")
        if abs(now - issued) > FRESHNESS_S or now >= expires:
            raise PayloadRejected("stale")
    elif now >= deadline:
        raise PayloadRejected("offline_deadline")
    return VerifiedBundle(policy=policy, device_generation=gen)


def verify_release_payload(p: dict, ident: ClientIdentity, request_id_b64: str, now: int, highest_release_seq: int,
                           stored_policy: MinPolicy, device_registered: bool,
                           stored_generation: int | None) -> VerifiedRelease:
    seq = _v(validate_release_seq, p.get("release_seq"))
    bundle_hash = _v(validate_bundle_hash, p.get("bundle_hash"))
    policy = merge_min_policy(stored_policy, _policy_of(p))
    _identity(p, ident, with_runtime=False)
    if p.get("request_id") != request_id_b64:
        raise PayloadRejected("request_mismatch")
    _short_lived(p, now)
    gen = _generation(p.get("device_generation"), required=device_registered, stored=stored_generation)
    if seq < highest_release_seq:
        raise PayloadRejected("downgrade")
    return VerifiedRelease(policy=policy, release_seq=seq, bundle_hash=bundle_hash, device_generation=gen)


def verify_revocation_payload(p: dict, ident: ClientIdentity, request_id_b64: str, now: int,
                              stored_generation: int | None) -> str:
    scope = p.get("scope")
    if (scope, p.get("error")) not in (("key", "key_revoked"), ("device", "device_revoked")):
        raise PayloadRejected("malformed")
    _v(validate_epoch, p.get("revoked_at"))
    fp = _v(b64d, p.get("key_fp"), 32)
    _v(hexd, p.get("device_id"), 32)
    if fp != ident.key_fp or p.get("device_id") != ident.device_id_hex:
        raise PayloadRejected("identity")
    if p.get("request_id") != request_id_b64:
        raise PayloadRejected("request_mismatch")
    _short_lived(p, now)
    if scope == "device":
        _generation(p.get("device_generation"), required=True, stored=stored_generation)
    return scope
