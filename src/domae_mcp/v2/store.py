"""도매 v2 로컬 저장소 (스펙 B1 다운그레이드 방지·B2 원자적 쓰기·B5 세대·B6 설치 nonce).

모든 파일은 앱 홈(P4) 아래 v2/ 에 두고 atomic_write_bytes 로만 쓴다. 보호가 필요한 파일은 Protector 로 감싼다.
"""
import json
import os
import threading
from dataclasses import asdict, dataclass, fields, replace
from pathlib import Path
from typing import Callable

from .device import NONCE_LEN
from .encoding import EncodingError, _is_int, b64d, b64e, validate_safe_int
from .winprotect import Protector, ProtectError, atomic_write_bytes, purpose, quarantine

_STATE_FORMAT = 1
_CACHE_FORMAT = 1
_ENVELOPE_FIELDS = ("payload", "signing_key_id", "signature")


class StoreError(Exception):
    """reason: install_unreadable | state_unreadable | monotonic | forgotten | not_fresh | invalid"""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def app_home() -> Path:
    """앱 홈(P4). ConfigManager·SQLite DB·v2 파일이 모두 이 경로를 쓴다."""
    env = os.environ.get("MAIPHARM_DOMAE_HOME")
    return Path(env) if env else Path.home() / ".maipharm-domae-mcp"


def data_dir() -> Path:
    d = app_home() / "v2"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _dumps(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


class InstallStore:
    """설치 nonce(P6). 잃으면 새 device_id 가 된다(D6)."""

    def __init__(self, root: Path, prot: Protector):
        self._path = Path(root) / "install.dpapi"
        self._prot = prot
        self._nonce: bytes | None = None
        self._lock = threading.Lock()

    def nonce(self) -> bytes:
        with self._lock:
            if self._nonce is not None:
                return self._nonce
            if self._path.exists():
                try:
                    value = self._prot.unprotect(self._path.read_bytes(), purpose("install"))
                except (ProtectError, OSError):
                    value = None
                if value is None or len(value) != NONCE_LEN:
                    quarantine(self._path)
                    raise StoreError("install_unreadable")
                self._nonce = value
                return value
            value = os.urandom(NONCE_LEN)
            atomic_write_bytes(self._path, self._prot.protect(value, purpose("install")))
            self._nonce = value
            return value


@dataclass
class KeyState:
    highest_release_seq: int = 0
    min_release_seq: int = 1
    min_protocol_version: int = 1
    device_generation: int | None = None
    revoked: dict | None = None             # {"scope": "key"|"device", "at": epoch}
    time_high_water: int = 0
    backoff_until: int = 0
    backoff_level: int = 0


MONOTONIC = ("highest_release_seq", "min_release_seq", "min_protocol_version", "device_generation", "time_high_water")
_FRESH_S = 600
_MINIMUM = {"min_release_seq": 1, "min_protocol_version": 1}


def _check_state(s: KeyState) -> KeyState:
    """정수 자리는 안전 정수만(bool·float 거부). 틀리면 EncodingError."""
    for f in fields(KeyState):
        v = getattr(s, f.name)
        if f.name == "revoked":
            if v is None:
                continue
            if not isinstance(v, dict) or set(v) != {"scope", "at"} or v["scope"] not in ("key", "device"):
                raise EncodingError("revoked")
            validate_safe_int(v["at"], 0, "revoked")
        elif f.name == "device_generation" and v is None:
            continue
        else:
            validate_safe_int(v, _MINIMUM.get(f.name, 0), f.name)
    return s


def _copy(s: KeyState) -> KeyState:
    return replace(s, revoked=dict(s.revoked) if s.revoked is not None else None)


def _fp(key_fp: bytes) -> str:
    if not isinstance(key_fp, (bytes, bytearray)) or len(key_fp) != 32:
        raise StoreError("invalid")
    return bytes(key_fp).hex()


def _state_from(d) -> KeyState:
    if not isinstance(d, dict) or set(d) != {f.name for f in fields(KeyState)}:
        raise EncodingError("state")
    return _check_state(KeyState(**d))


class StateStore:
    """키별 단조 상태(P7). {"format":1,"keys":{key_fp_hex: KeyState}} 를 DPAPI 로 감싸 둔다."""

    def __init__(self, root: Path, prot: Protector):
        self._path = Path(root) / "state.dpapi"
        self._prot = prot
        self._lock = threading.RLock()
        self._keys: dict[str, KeyState] | None = None

    def _load(self) -> dict[str, KeyState]:
        if self._keys is not None:
            return self._keys
        if not self._path.exists():
            self._keys = {}
            return self._keys
        try:
            raw = json.loads(self._prot.unprotect(self._path.read_bytes(), purpose("state")).decode("utf-8"))
            if not isinstance(raw, dict) or not _is_int(raw.get("format")) or raw.get("format") != _STATE_FORMAT or not isinstance(raw.get("keys"), dict):
                raise EncodingError("state")
            keys = {}
            for k, v in raw["keys"].items():
                if not isinstance(k, str) or len(k) != 64:
                    raise EncodingError("state")
                keys[k] = _state_from(v)
        except (ProtectError, OSError, ValueError, TypeError, EncodingError):
            quarantine(self._path)
            self._keys = {}
            raise StoreError("state_unreadable") from None
        self._keys = keys
        return keys

    def _write(self, keys: dict[str, KeyState]) -> None:
        body = _dumps({"format": _STATE_FORMAT, "keys": {k: asdict(v) for k, v in keys.items()}})
        atomic_write_bytes(self._path, self._prot.protect(body, purpose("state")))

    def get(self, key_fp: bytes) -> KeyState:
        with self._lock:
            s = self._load().get(_fp(key_fp))
            return _copy(s) if s is not None else KeyState()

    def has(self, key_fp: bytes) -> bool:
        with self._lock:
            return _fp(key_fp) in self._load()

    def update(self, key_fp: bytes, fn: Callable[[KeyState], KeyState], create: bool = True) -> KeyState:
        with self._lock:
            name = _fp(key_fp)
            keys = self._load()
            old = keys.get(name)
            if old is None and not create:
                raise StoreError("forgotten")
            base = old if old is not None else KeyState()
            new = fn(_copy(base))
            try:
                _check_state(new)
            except EncodingError:
                raise StoreError("invalid") from None
            for f in MONOTONIC:
                before, after = getattr(base, f), getattr(new, f)
                if before is not None and (after is None or after < before):
                    raise StoreError("monotonic")
            nxt = dict(keys)
            nxt[name] = _copy(new)
            self._write(nxt)
            self._keys = nxt
            return _copy(new)

    def reset_high_water(self, key_fp: bytes, verified_issued_at: int, now: int, create: bool = False) -> KeyState:
        """시계 복구(P8, 결정 A). 서명·request_id·±10분 신선도를 통과한 release 응답의 issued_at 만 넘긴다.
        로컬 now 가 서버 시각과 ±10분 안임을 확인한 뒤 time_high_water 를 now 로 둔다. 낮출 수 있는 유일한 연산이다."""
        if abs(now - verified_issued_at) > _FRESH_S:
            raise StoreError("not_fresh")
        with self._lock:
            name = _fp(key_fp)
            keys = self._load()
            if name not in keys and not create:
                raise StoreError("forgotten")
            s = _copy(keys.get(name) or KeyState())
            s.time_high_water = now
            try:
                _check_state(s)
            except EncodingError:
                raise StoreError("invalid") from None
            nxt = dict(keys)
            nxt[name] = s
            self._write(nxt)
            self._keys = nxt
            return _copy(s)

    def forget_others(self, key_fp: bytes) -> None:
        with self._lock:
            name = _fp(key_fp)
            keys = self._load()
            nxt = {name: keys[name]} if name in keys else {}
            if nxt != keys:
                self._write(nxt)
                self._keys = nxt

    def forget(self, key_fp: bytes) -> None:
        with self._lock:
            name = _fp(key_fp)
            keys = self._load()
            if name in keys:
                nxt = {k: v for k, v in keys.items() if k != name}
                self._write(nxt)
                self._keys = nxt


class BundleCache:
    """서명 envelope 원문 + 감싼 세션 키. envelope 은 이미 서명·모듈 암호화돼 있어 파일은 다시 감싸지 않는다(스펙 B2)."""

    def __init__(self, root: Path):
        self._path = Path(root) / "bundle-cache.json"

    @property
    def path(self) -> Path:
        return self._path

    def read(self) -> dict | None:
        if not self._path.exists():
            return None
        try:
            raw = json.loads(self._path.read_bytes().decode("utf-8"))
            if not isinstance(raw, dict) or set(raw) != {"format", "envelope", "wrapped_session_key"} \
                    or not _is_int(raw["format"]) or raw["format"] != _CACHE_FORMAT:
                raise ValueError("format")
            env = raw["envelope"]
            if not isinstance(env, dict) or set(env) != set(_ENVELOPE_FIELDS) \
                    or not all(isinstance(env[f], str) for f in _ENVELOPE_FIELDS):
                raise ValueError("envelope")
            wrapped = b64d(raw["wrapped_session_key"])
        except (OSError, ValueError, TypeError, EncodingError):
            quarantine(self._path)
            return None
        return {"envelope": {f: env[f] for f in _ENVELOPE_FIELDS}, "wrapped_session_key": wrapped}

    def write(self, envelope: dict, wrapped: bytes) -> None:
        env = {f: envelope[f] for f in _ENVELOPE_FIELDS}
        atomic_write_bytes(self._path, _dumps({"format": _CACHE_FORMAT, "envelope": env,
                                               "wrapped_session_key": b64e(wrapped)}))

    def delete(self) -> None:
        try:
            self._path.unlink()
        except FileNotFoundError:
            pass
