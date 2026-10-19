"""도매 v2 로더 조립 — network·cache 모드 (스펙 B1 클라이언트 검증·B2·B5, P8·P11·P13·P14·P15·P20).

순서: envelope 서명 → payload 검증 → 정책 저장 → check_loaded → (cache 모드) DPAPI 해제 → GCM → manifest 검사.
이 모듈은 크롤러를 실행하지 않는다(marshal.loads·exec 없음). 실행은 gate.RuntimeGate.publish 가 게이트 잠금 안에서 한다.
보안 정책 쓰기(최소 정책·최고 release_seq·세대·시각·철회)는 그 key_fp 항목에만 단조로 한다(되돌리지 않는다).
"""
import logging
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, PublicFormat

from . import client as c
from .artifacts import MAX_ARTIFACT_BYTES, LoadFailed, check_manifest, manifest_sha256
from .bundle_crypto import PROTOCOL_VERSION, BundleContext, CryptoError, decrypt_modules, derive_session_key, x25519_shared
from .clock import Clock, ClockUntrusted, effective_now_cache, next_high_water
from .encoding import EncodingError, b64d, b64e, key_fp_from_bearer, u64, validate_epoch
from .envelope import DOMAIN_BUNDLE, DOMAIN_RELEASE, DOMAIN_REVOCATION, EnvelopeError, verify_envelope
from .runtime import local_runtime_id
from .store import BundleCache, KeyState, StateStore, StoreError
from .verify import (ClientIdentity, MinPolicy, PayloadRejected, PendingRequest, check_loaded, parse_payload,
                     verify_bundle_payload, verify_release_payload, verify_revocation_payload)
from .winprotect import Protector, ProtectError, purpose, quarantine

logger = logging.getLogger(__name__)

__all__ = ["MAX_ARTIFACT_BYTES", "Prepared", "VersionInfo", "V2Error", "V2Revoked", "V2Loader"]


@dataclass(frozen=True)
class Prepared:
    key_fp: bytes = field(repr=False)
    plain: dict = field(repr=False)
    manifest: dict = field(repr=False)
    release_seq: int
    bundle_hash: str
    issued_at: int
    expires_at: int
    offline_deadline: int
    device_generation: int
    request_id_b64: str
    mode: str
    envelope: dict | None = field(default=None, repr=False)
    wrapped_session_key: bytes | None = field(default=None, repr=False)

    @property
    def issuance(self) -> tuple[str, int]:
        return self.request_id_b64, self.issued_at


@dataclass(frozen=True)
class VersionInfo:
    release_seq: int
    bundle_hash: str
    min_release_seq: int
    min_protocol_version: int
    device_generation: int | None


class V2Error(Exception):
    def __init__(self, reason: str, retry_after: int | None = None, detail: str | None = None):
        super().__init__(reason if detail is None else f"{reason}:{detail}")
        self.reason = reason
        self.retry_after = retry_after
        self.detail = detail


class V2Revoked(V2Error):
    """검증된 철회. 캐시 fallback 금지."""


def _session_purpose(key_fp: bytes, bundle_hash: str, release_seq: int) -> bytes:
    return purpose("session-key", key_fp, bundle_hash.encode("utf-8"), u64(release_seq))


def _policy(ks: KeyState) -> MinPolicy:
    return MinPolicy(ks.min_release_seq, ks.min_protocol_version)


class V2Loader:
    def __init__(self, bearer: str, cache_dir: Path, state: StateStore, prot: Protector,
                 client: c.V2Client | None = None, clock: Clock | None = None, trust: dict[str, bytes] | None = None,
                 still_current: Callable[[], bool] = lambda: True,
                 policy_write: Callable[[bytes, Callable[[KeyState], KeyState]], KeyState] | None = None,
                 device_id_hex: str | None = None, runtime_id: dict | None = None):
        self.key_fp = key_fp_from_bearer(bearer)
        self._cache = BundleCache(cache_dir)
        self._state = state
        self._prot = prot
        self._clock = clock or Clock()
        if trust is None:
            from .trust import active_trust
            trust = active_trust()
        self._trust = trust
        self._still_current = still_current
        self._policy_write = policy_write
        if device_id_hex is None:
            from .device import device_id_hex as _dev
            from .store import InstallStore, data_dir
            device_id_hex = _dev(InstallStore(data_dir(), prot).nonce())
        self.device_id_hex = device_id_hex
        self._runtime_id = runtime_id or local_runtime_id()
        self._ident = ClientIdentity(self.key_fp, device_id_hex, self._runtime_id)
        self._client = client or c.V2Client(bearer, device_id_hex)
        self._version_verified = False

    # ── 상태 쓰기 ──

    def _write(self, fn: Callable[[KeyState], KeyState]) -> KeyState:
        """그 key_fp 항목에만 쓴다. 현재 컨텍스트가 아니면 지운 항목을 되살리지 않는다(P20)."""
        if self._policy_write is not None:
            return self._policy_write(self.key_fp, fn)
        return self._state.update(self.key_fp, fn, create=self._still_current())

    def _write_quiet(self, fn: Callable[[KeyState], KeyState]) -> None:
        try:
            self._write(fn)
        except Exception as e:
            logger.warning("v2 상태 쓰기 생략: %s", getattr(e, "reason", type(e).__name__))

    def fail_backoff(self, retry_after: int | None = None) -> None:
        """다운로드 실패(HTTP 오류·200 뒤 검증 실패·publish 실패) 뒤 backoff 를 올려 저장한다(P10)."""
        now = self._clock.wall()

        def fn(s: KeyState) -> KeyState:
            s.backoff_until, s.backoff_level = c.next_backoff(s.backoff_level, now, retry_after, random.random)
            return s

        self._write_quiet(fn)

    def reset_backoff(self) -> None:
        """새 network 발급본의 전체 검증·로드·publish 성공 뒤에만 부른다."""
        def fn(s: KeyState) -> KeyState:
            s.backoff_until, s.backoff_level = 0, 0
            return s

        self._write_quiet(fn)

    def close(self) -> None:
        self._client.close()

    # ── 철회 ──

    def _revocation(self, env: dict, request_id_b64: str) -> None:
        """서명·payload 를 통과한 403 만 철회다. 실패는 auth_error 로 돌려준다(호출자가 처리)."""
        now = self._clock.wall()
        try:
            stored_generation = self._state.get(self.key_fp).device_generation
        except StoreError:  # 상태를 못 읽어도 철회 검증은 계속한다
            stored_generation = None
        try:
            p = parse_payload(verify_envelope(env, DOMAIN_REVOCATION, self._trust))
            scope = verify_revocation_payload(p, self._ident, request_id_b64, now, stored_generation)
        except (EnvelopeError, PayloadRejected):
            raise V2Error(c.AUTH_ERROR, detail="revocation_unverified") from None
        if not self._still_current():
            raise V2Error("stale")

        def fn(s: KeyState) -> KeyState:
            s.revoked = {"scope": scope, "at": now}
            return s

        # 상태 쓰기·캐시 삭제가 실패해도 철회는 철회다(캐시 fallback 으로 새지 않게 항상 V2Revoked)
        try:
            self._write(fn)
        except Exception as e:
            logger.warning("v2 철회 상태 저장 실패: %s", getattr(e, "reason", type(e).__name__))
        try:
            self._cache.delete()
        except OSError:
            try:
                quarantine(self._cache.path)
            except OSError:
                logger.warning("v2 철회 뒤 캐시를 지우지 못했다")
        self._version_verified = False
        raise V2Revoked("key_revoked" if scope == "key" else "device_revoked")

    # ── 버전 확인 ──

    def check_version(self) -> VersionInfo:
        req = self._client.new_request()
        o = self._client.version(req)
        if o.cancelled:
            raise V2Error("cancelled")
        if o.kind == c.REVOKED_ENVELOPE:
            self._revocation(o.envelope, req.request_id_b64)
        if o.kind != c.OK:
            raise V2Error(o.kind, retry_after=o.retry_after, detail=o.error)
        now = self._clock.wall()
        ks = self._state.get(self.key_fp)
        try:
            p = parse_payload(verify_envelope(o.envelope, DOMAIN_RELEASE, self._trust))
            vr = verify_release_payload(p, self._ident, req.request_id_b64, now, ks.highest_release_seq, _policy(ks),
                                        device_registered=ks.device_generation is not None,
                                        stored_generation=ks.device_generation)
            issued = validate_epoch(p["issued_at"])
        except EnvelopeError as e:
            raise V2Error("envelope", detail=e.reason) from None
        except (PayloadRejected, EncodingError) as e:
            raise V2Error("payload", detail=getattr(e, "reason", None)) from None
        if not self._still_current():
            raise V2Error("stale")

        def fn(s: KeyState) -> KeyState:
            s.min_release_seq = max(s.min_release_seq, vr.policy.min_release_seq)
            s.min_protocol_version = max(s.min_protocol_version, vr.policy.min_protocol_version)
            if vr.device_generation is not None and (s.device_generation is None
                                                     or vr.device_generation > s.device_generation):
                s.device_generation = vr.device_generation
            return s

        ks = self._write(fn)
        try:
            # 시계 복구(P8): 검증된 서버 시각으로 time_high_water 를 재설정하는 유일한 경로.
            # 시각 기준은 차단 판정(세대·정책)과 무관하므로 policy_write 를 거치지 않는다. 차단 해제는 게이트가
            # check_version 성공 뒤 잠금 안에서 한다(gate._recover_clock).
            self._state.reset_high_water(self.key_fp, issued, now, create=self._still_current())
        except StoreError as e:
            logger.warning("v2 시각 기준 재설정 생략: %s", e.reason)
        self._version_verified = True
        if PROTOCOL_VERSION < ks.min_protocol_version:
            raise V2Error("protocol_too_old")
        return VersionInfo(vr.release_seq, vr.bundle_hash, ks.min_release_seq, ks.min_protocol_version,
                           vr.device_generation)

    def needs_download(self, v: VersionInfo, cached: Prepared | None) -> bool:
        if cached is None:
            return True
        if cached.release_seq == v.release_seq and cached.bundle_hash != v.bundle_hash:
            raise V2Error("bundle_hash_conflict")
        return (cached.release_seq != v.release_seq
                or cached.expires_at <= self._clock.wall()
                or cached.release_seq < v.min_release_seq
                or (v.device_generation is not None and v.device_generation > cached.device_generation))

    # ── network 모드 ──

    def prepare_download(self) -> Prepared:
        now = self._clock.wall()
        ks = self._state.get(self.key_fp)
        if ks.backoff_until > now:
            raise V2Error("backoff", retry_after=ks.backoff_until - now)
        priv = X25519PrivateKey.generate()
        priv_raw = bytearray(priv.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption()))
        client_pub = priv.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        req = self._client.new_request()
        o = self._client.bundle(req, b64e(client_pub), self._runtime_id)
        try:
            if o.kind == c.REVOKED_ENVELOPE:
                try:
                    self._revocation(o.envelope, req.request_id_b64)
                except V2Revoked:
                    raise
                except V2Error as e:
                    if e.reason != "stale":
                        self.fail_backoff()
                    raise
            if o.cancelled:  # 우리가 취소한 요청은 서버 실패가 아니다(backoff 없음)
                raise V2Error("cancelled")
            if o.kind != c.OK:
                self.fail_backoff(o.retry_after)
                raise V2Error(o.kind, retry_after=o.retry_after, detail=o.error)
            try:
                return self._verify_download(o.envelope, req, bytes(priv_raw), client_pub)
            except V2Error as e:
                if e.reason != "stale":
                    self.fail_backoff()
                raise
            except Exception as e:  # 200 뒤 저장·DPAPI 실패도 backoff(P10)
                self.fail_backoff()
                raise V2Error("store", detail=type(e).__name__) from None
        finally:
            for i in range(len(priv_raw)):
                priv_raw[i] = 0

    def _verify_download(self, env: dict, req: c.Request, priv_raw: bytes, client_pub: bytes) -> Prepared:
        now = self._clock.wall()
        ks = self._state.get(self.key_fp)
        try:
            p = parse_payload(verify_envelope(env, DOMAIN_BUNDLE, self._trust))
            vb = verify_bundle_payload(p, self._ident, "network", now, ks.highest_release_seq, _policy(ks),
                                       ks.device_generation, PendingRequest(req.request_id_b64, b64e(client_pub)))
        except EnvelopeError as e:
            raise V2Error("envelope", detail=e.reason) from None
        except PayloadRejected as e:
            raise V2Error("payload", detail=e.reason) from None
        if not self._still_current():
            raise V2Error("stale")
        seq = p["release_seq"]

        def fn(s: KeyState) -> KeyState:
            s.min_release_seq = max(s.min_release_seq, vb.policy.min_release_seq)
            s.min_protocol_version = max(s.min_protocol_version, vb.policy.min_protocol_version)
            if s.device_generation is None or vb.device_generation > s.device_generation:
                s.device_generation = vb.device_generation
            s.time_high_water = next_high_water(s.time_high_water, now)
            return s

        ks = self._write(fn)
        try:
            check_loaded(_policy(ks), seq)
        except PayloadRejected as e:
            raise V2Error(e.reason) from None
        try:
            server_pub, salt = b64d(p.get("server_pub"), 32), b64d(p.get("salt"), 32)
        except EncodingError:
            raise V2Error("payload", detail="malformed") from None
        ctx = BundleContext(PROTOCOL_VERSION, b64d(req.request_id_b64, 16), client_pub, server_pub, self.key_fp,
                            bytes.fromhex(self.device_id_hex), p["bundle_hash"], seq, self._runtime_id)
        try:
            session = derive_session_key(x25519_shared(priv_raw, server_pub), salt, ctx)
        except CryptoError as e:
            raise V2Error("crypto", detail=e.reason) from None
        plain = self._open_modules(p, session, ctx)
        wrapped = self._prot.protect(session, _session_purpose(self.key_fp, p["bundle_hash"], seq))
        clear_revoked = ks.revoked is not None and self._version_verified

        def accepted(s: KeyState) -> KeyState:
            # 최고 release_seq 는 복호화까지 성공한 번들만 올린다. 200 뒤 GCM 실패가 아직 유효한 캐시를 막지 않게 한다
            s.highest_release_seq = max(s.highest_release_seq, seq)
            if clear_revoked:
                s.revoked = None
            return s

        self._write(accepted)
        return self._prepared(p, plain, "network", env, wrapped)

    def _open_modules(self, p: dict, session: bytes, ctx: BundleContext) -> dict:
        manifest, modules = p.get("manifest"), p.get("modules")
        if not isinstance(modules, dict):
            raise V2Error("payload", detail="malformed")
        try:
            check_manifest(manifest, set(modules), self._runtime_id)  # 모듈당 크기 상한 포함(복호화 전)
        except LoadFailed as e:
            raise V2Error("load", detail=e.reason) from None
        try:
            return decrypt_modules(session, ctx, modules, manifest_sha256(manifest))
        except CryptoError as e:
            raise V2Error("crypto", detail=e.reason) from None

    def _prepared(self, p: dict, plain: dict, mode: str, env: dict | None, wrapped: bytes | None) -> Prepared:
        return Prepared(self.key_fp, plain, p["manifest"], p["release_seq"], p["bundle_hash"], p["issued_at"],
                        p["expires_at"], p["offline_deadline"], p["device_generation"], p["request_id"], mode,
                        env, wrapped)

    def write_cache(self, p: Prepared) -> bool:
        if p.mode != "network" or p.envelope is None or p.wrapped_session_key is None:
            return False
        if not self._still_current():
            return False
        self._cache.write(p.envelope, p.wrapped_session_key)
        return True

    # ── cache 모드 ──

    def prepare_cached(self) -> Prepared:
        ks = self._state.get(self.key_fp)
        if ks.revoked is not None:
            raise V2Revoked("key_revoked" if ks.revoked["scope"] == "key" else "device_revoked")
        cached = self._cache.read()
        if cached is None:
            raise V2Error("no_cache")
        env = cached["envelope"]
        now = self._clock.wall()
        try:
            p = parse_payload(verify_envelope(env, DOMAIN_BUNDLE, self._trust))
            issued = validate_epoch(p.get("issued_at"))
        except EnvelopeError as e:
            quarantine(self._cache.path)
            raise V2Error("envelope", detail=e.reason) from None
        except (PayloadRejected, EncodingError):
            quarantine(self._cache.path)
            raise V2Error("payload", detail="malformed") from None
        try:
            eff = effective_now_cache(now, ks.time_high_water, issued)
        except ClockUntrusted:
            raise V2Error("clock") from None
        try:
            vb = verify_bundle_payload(p, self._ident, "cache", eff, ks.highest_release_seq, _policy(ks),
                                       ks.device_generation)
        except PayloadRejected as e:
            raise V2Error("payload", detail=e.reason) from None
        if not self._still_current():
            raise V2Error("stale")

        def fn(s: KeyState) -> KeyState:
            s.min_release_seq = max(s.min_release_seq, vb.policy.min_release_seq)
            s.min_protocol_version = max(s.min_protocol_version, vb.policy.min_protocol_version)
            s.time_high_water = next_high_water(s.time_high_water, now)
            return s

        ks = self._write(fn)
        try:
            check_loaded(_policy(ks), p["release_seq"])
        except PayloadRejected as e:
            raise V2Error(e.reason) from None
        # 여기서 처음으로 DPAPI 를 푼다(서명·컨텍스트 검증 뒤)
        try:
            session = self._prot.unprotect(cached["wrapped_session_key"],
                                           _session_purpose(self.key_fp, p["bundle_hash"], p["release_seq"]))
        except ProtectError:
            quarantine(self._cache.path)
            raise V2Error("store") from None
        if len(session) != 32:
            quarantine(self._cache.path)
            raise V2Error("store")
        try:
            ctx = BundleContext(PROTOCOL_VERSION, b64d(p.get("request_id"), 16), b64d(p.get("client_pub"), 32),
                                b64d(p.get("server_pub"), 32), self.key_fp, bytes.fromhex(self.device_id_hex),
                                p["bundle_hash"], p["release_seq"], self._runtime_id)
        except EncodingError:
            quarantine(self._cache.path)
            raise V2Error("payload", detail="malformed") from None
        try:
            plain = self._open_modules(p, session, ctx)
        except V2Error:
            quarantine(self._cache.path)
            raise
        return self._prepared(p, plain, "cache", None, None)
