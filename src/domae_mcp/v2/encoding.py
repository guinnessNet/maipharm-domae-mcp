"""도매 v2 인코딩 규약 (스펙 B1). server src/lib/domaeV2/encoding.ts 와 같은 바이트를 만든다."""
import base64
import binascii
import hashlib
import re
import struct

MAX_SAFE = 2**53 - 1
_HEX = re.compile(r"\A[0-9a-f]*\Z")
_RT_FIELDS = ("implementation", "python_version", "magic", "artifact_kind", "loader_abi", "platform")


class EncodingError(ValueError):
    pass


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def lp(b: bytes) -> bytes:
    if not isinstance(b, (bytes, bytearray)) or len(b) > 0xFFFFFFFF:
        raise EncodingError("lp")
    return struct.pack(">I", len(b)) + bytes(b)


def u64(n: int) -> bytes:
    if not _is_int(n) or n < 0 or n > 0xFFFFFFFFFFFFFFFF:
        raise EncodingError("u64")
    return struct.pack(">Q", n)


def b64e(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")


def b64d(s: str, size: int | None = None) -> bytes:
    if not isinstance(s, str):
        raise EncodingError("b64")
    try:
        out = base64.b64decode(s.encode("ascii"), validate=True)
    except (binascii.Error, UnicodeEncodeError, ValueError):
        raise EncodingError("b64") from None
    if b64e(out) != s or (size is not None and len(out) != size):
        raise EncodingError("b64")
    return out


def hexd(s: str, size: int) -> bytes:
    if not isinstance(s, str) or len(s) != size * 2 or not _HEX.match(s):
        raise EncodingError("hex")
    return bytes.fromhex(s)


def validate_safe_int(v, minimum: int, what: str) -> int:
    """minimum 이상 2^53-1 이하의 정수만 받는다. bool·float·문자열은 거부한다(TypeScript Number.isSafeInteger 와 같은 범위)."""
    if not _is_int(v) or v < minimum or v > MAX_SAFE:
        raise EncodingError(what)
    return v


def validate_release_seq(v) -> int:
    return validate_safe_int(v, 1, "release_seq")


def validate_epoch(v) -> int:
    return validate_safe_int(v, 0, "epoch")


def validate_protocol_version(v) -> int:
    return validate_safe_int(v, 1, "protocol_version")


def validate_bundle_hash(v) -> str:
    if not isinstance(v, str) or len(v) != 64 or not _HEX.match(v):
        raise EncodingError("bundle_hash")
    return v


def encode_runtime_id(rt: dict) -> bytes:
    if not isinstance(rt, dict) or set(rt) != set(_RT_FIELDS):
        raise EncodingError("runtime_id")
    for k in ("implementation", "python_version", "artifact_kind", "platform"):
        if not isinstance(rt[k], str) or not rt[k]:
            raise EncodingError("runtime_id")
    if not isinstance(rt["magic"], str) or len(rt["magic"]) % 2 or not rt["magic"]:
        raise EncodingError("runtime_id")
    magic = hexd(rt["magic"], len(rt["magic"]) // 2)
    return (lp(rt["implementation"].encode()) + lp(rt["python_version"].encode()) + lp(magic)
            + lp(rt["artifact_kind"].encode()) + lp(u64(validate_safe_int(rt["loader_abi"], 0, "runtime_id")))
            + lp(rt["platform"].encode()))


def key_fp_from_bearer(bearer: str) -> bytes:
    if not isinstance(bearer, str) or not bearer:
        raise EncodingError("bearer")
    return hashlib.sha256(bearer.encode("utf-8")).digest()


def key_fp_from_hex(h: str) -> bytes:
    return hexd(h, 32)
