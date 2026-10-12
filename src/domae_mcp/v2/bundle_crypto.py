"""도매 v2 키 교환·세션 키·모듈 AES-GCM (스펙 B1 서버 처리·클라이언트 검증)."""
import hashlib
from dataclasses import dataclass

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from .encoding import (EncodingError, b64d, b64e, encode_runtime_id, lp, u64, validate_bundle_hash,
                       validate_protocol_version, validate_release_seq)

PROTOCOL_VERSION = 2


class CryptoError(ValueError):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class BundleContext:
    protocol_version: int
    request_id: bytes
    client_pub: bytes
    server_pub: bytes
    key_fp: bytes
    device_id: bytes
    bundle_hash: str
    release_seq: int
    runtime_id: dict


def _check(ctx: BundleContext) -> bytes:
    sizes = {"request_id": 16, "client_pub": 32, "server_pub": 32, "key_fp": 32, "device_id": 32}
    for name, size in sizes.items():
        if len(getattr(ctx, name)) != size:
            raise CryptoError("context")
    try:
        validate_protocol_version(ctx.protocol_version)
        validate_release_seq(ctx.release_seq)
        validate_bundle_hash(ctx.bundle_hash)
        return encode_runtime_id(ctx.runtime_id)
    except EncodingError:
        raise CryptoError("context") from None


def kdf_info(ctx: BundleContext) -> bytes:
    rt = _check(ctx)
    return (lp(b"domae-crawler-v2") + lp(u64(ctx.protocol_version)) + lp(ctx.request_id) + lp(ctx.client_pub)
            + lp(ctx.server_pub) + lp(ctx.key_fp) + lp(ctx.device_id) + lp(ctx.bundle_hash.encode("utf-8"))
            + lp(u64(ctx.release_seq)) + lp(rt))


def module_aad(ctx: BundleContext, module_name: str, artifact_sha256: bytes) -> bytes:
    rt = _check(ctx)
    if len(artifact_sha256) != 32:
        raise CryptoError("context")
    return (lp(u64(ctx.protocol_version)) + lp(ctx.request_id) + lp(ctx.key_fp) + lp(ctx.device_id)
            + lp(ctx.bundle_hash.encode("utf-8")) + lp(u64(ctx.release_seq)) + lp(rt)
            + lp(module_name.encode("utf-8")) + lp(artifact_sha256))


def x25519_shared(private_raw: bytes, peer_pub: bytes) -> bytes:
    if len(private_raw) != 32 or len(peer_pub) != 32:
        raise CryptoError("key_length")
    try:
        shared = X25519PrivateKey.from_private_bytes(private_raw).exchange(X25519PublicKey.from_public_bytes(peer_pub))
    except ValueError:  # cryptography 는 low-order 점에서 ValueError 를 낸다
        raise CryptoError("zero_shared") from None
    if shared == b"\x00" * 32:
        raise CryptoError("zero_shared")
    return shared


def derive_session_key(shared: bytes, salt: bytes, ctx: BundleContext) -> bytes:
    if len(salt) != 32:
        raise CryptoError("salt")
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=salt, info=kdf_info(ctx)).derive(shared)


def encrypt_module(session_key: bytes, ctx: BundleContext, name: str, plain: bytes, iv: bytes) -> dict:
    if len(iv) != 12:
        raise CryptoError("iv")
    out = AESGCM(session_key).encrypt(iv, plain, module_aad(ctx, name, hashlib.sha256(plain).digest()))
    return {"iv": b64e(iv), "ct": b64e(out[:-16]), "tag": b64e(out[-16:])}


def decrypt_modules(session_key: bytes, ctx: BundleContext, modules: dict, manifest_sha256: dict) -> dict:
    if not isinstance(modules, dict) or set(modules) != set(manifest_sha256):
        raise CryptoError("module_set")
    seen, out = set(), {}
    for name in sorted(modules):
        entry = modules[name]
        try:
            iv, ct, tag = b64d(entry["iv"], 12), b64d(entry["ct"]), b64d(entry["tag"], 16)
        except (EncodingError, KeyError, TypeError):
            raise CryptoError("module_format") from None
        if iv in seen:
            raise CryptoError("duplicate_iv")
        seen.add(iv)
        sha = manifest_sha256[name]
        try:
            plain = AESGCM(session_key).decrypt(iv, ct + tag, module_aad(ctx, name, sha))
        except InvalidTag:
            raise CryptoError("gcm") from None
        if hashlib.sha256(plain).digest() != sha:
            raise CryptoError("artifact_sha256")
        out[name] = plain
    return out
