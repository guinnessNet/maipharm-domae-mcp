"""도매 v2 HTTP 클라이언트·응답 분류·backoff (스펙 B1 헤더, B9 backoff, B-3 인계 오류 처리, P1·P12).

응답 본문·bearer 는 로그에 쓰지 않는다. 연결 오류·타임아웃은 예외를 밖으로 내지 않고 transient 로 돌려준다.
"""
import base64
import json
import os
from dataclasses import dataclass, field
from typing import Callable, Mapping

import httpx

from domae_mcp import __version__

from .bundle_crypto import PROTOCOL_VERSION
from .runtime import dev_override

SERVER_BASE = "https://pharmsq.com/api/domae"
ORIGIN = "https://pharmsq.com"
TIMEOUT = httpx.Timeout(10.0, read=60.0)
MAX_BACKOFF_S = 21600
BASE_BACKOFF_S = 300
_ENVELOPE_FIELDS = {"payload", "signing_key_id", "signature"}

OK = "ok"
REVOKED_ENVELOPE = "revoked_envelope"
AUTH_ERROR = "auth_error"
DEVICE_LIMIT = "device_limit"
RETRY_LATER = "retry_later"
RUNTIME_UNSUPPORTED = "runtime_unsupported"
RATE_LIMITED = "rate_limited"
TRANSIENT = "transient"


@dataclass(frozen=True)
class Request:
    request_id_b64: str
    device_id_hex: str = field(repr=False)


@dataclass(frozen=True)
class Outcome:
    kind: str
    envelope: dict | None = field(default=None, repr=False)
    retry_after: int | None = None
    error: str | None = None


def server_base() -> str:
    return (dev_override("DOMAE_V2_SERVER_BASE") or SERVER_BASE).rstrip("/")


def _json(body: bytes):
    try:
        return json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None


def _envelope(obj) -> dict | None:
    if isinstance(obj, dict) and set(obj) == _ENVELOPE_FIELDS and all(isinstance(obj[k], str) for k in _ENVELOPE_FIELDS):
        return dict(obj)
    return None


def _error_code(obj) -> str | None:
    if isinstance(obj, dict) and isinstance(obj.get("error"), str):
        return obj["error"][:64]
    return None


def _retry_after(headers: Mapping[str, str]) -> int | None:
    v = None
    for k, val in headers.items():
        if k.lower() == "retry-after":
            v = val
    if isinstance(v, str) and v.isascii() and v.isdigit() and 0 <= int(v) <= 86400:
        return int(v)
    return None


def classify(status: int, body: bytes, headers: Mapping[str, str]) -> Outcome:
    obj = _json(body)
    err = _error_code(obj)
    if status == 200:
        env = _envelope(obj)
        return Outcome(OK, envelope=env) if env else Outcome(TRANSIENT, error="bad_envelope")
    if status == 403:
        env = _envelope(obj)
        if env:
            return Outcome(REVOKED_ENVELOPE, envelope=env)
        if err == "device_limit":
            return Outcome(DEVICE_LIMIT, error=err)
        return Outcome(AUTH_ERROR, error=err)
    if status in (400, 401):
        return Outcome(AUTH_ERROR, error=err)
    if status == 409:
        if err == "client runtime unsupported":
            return Outcome(RUNTIME_UNSUPPORTED, error=err)
        return Outcome(RETRY_LATER, error=err)
    if status == 429:
        return Outcome(RATE_LIMITED, retry_after=_retry_after(headers), error=err)
    return Outcome(TRANSIENT, error=err or f"http_{status}")


def next_backoff(level: int, now: int, retry_after: int | None, rnd: Callable[[], float]) -> tuple[int, int]:
    base = min(MAX_BACKOFF_S, BASE_BACKOFF_S * 2 ** min(level, 16)) * (0.9 + 0.2 * rnd())
    until = now + int(max(base, retry_after or 0))
    return until, level + 1


class V2Client:
    def __init__(self, bearer: str, device_id_hex: str, client_version: str = __version__,
                 http: httpx.Client | None = None, base: str | None = None):
        # base 는 시험 전용이다. 운영 경로는 넘기지 않는다(주소는 dev_override 로만 바뀐다, P1)
        self._bearer = bearer
        self._device = device_id_hex
        self._version = client_version
        self._http = http or httpx.Client(follow_redirects=False, timeout=TIMEOUT)
        self._base = (base or server_base()).rstrip("/")
        self._closed = False

    def close(self) -> None:
        """진행 중 요청 취소(P20). 이후 요청은 transient."""
        self._closed = True
        try:
            self._http.close()
        except Exception:
            pass

    def new_request(self) -> Request:
        return Request(base64.b64encode(os.urandom(16)).decode("ascii"), self._device)

    def _headers(self, req: Request) -> dict:
        return {
            "Authorization": f"Bearer {self._bearer}",
            "X-Request-Id": req.request_id_b64,
            "X-Device-Id": req.device_id_hex,
            "X-Client-Version": self._version,
            "User-Agent": f"MaipharmDomae/{self._version}",
        }

    def _send(self, method: str, path: str, headers: dict, body: bytes | None = None) -> Outcome:
        if self._closed:
            return Outcome(TRANSIENT, error="cancelled")
        try:
            r = self._http.request(method, self._base + path, headers=headers, content=body, follow_redirects=False)
        except Exception:  # 연결·타임아웃·잘못된 URL·인코딩 — 밖으로 내지 않는다
            return Outcome(TRANSIENT, error="cancelled" if self._closed else "connection")
        if self._closed:  # 취소 뒤 도착한 응답은 버린다(P20)
            return Outcome(TRANSIENT, error="cancelled")
        return classify(r.status_code, r.content, r.headers)

    def version(self, req: Request) -> Outcome:
        return self._send("GET", "/crawlers/v2/version", self._headers(req))

    def bundle(self, req: Request, client_pub_b64: str, runtime_id: dict) -> Outcome:
        h = self._headers(req)
        h["Origin"] = ORIGIN
        h["Content-Type"] = "application/json"
        body = {"protocol_version": PROTOCOL_VERSION, "request_id": req.request_id_b64, "client_pub": client_pub_b64,
                "device_id": req.device_id_hex, "client_version": self._version, "runtime_id": runtime_id}
        return self._send("POST", "/crawlers/v2", h, json.dumps(body, separators=(",", ":")).encode("utf-8"))
