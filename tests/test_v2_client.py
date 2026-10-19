"""도매 v2 HTTP 클라이언트·응답 분류·backoff 시험 (계획 Task 5). httpx.MockTransport 만 쓴다."""
import json

import httpx
import pytest

from domae_mcp import __version__
from domae_mcp.v2 import client as client_mod
from domae_mcp.v2.client import (AUTH_ERROR, DEVICE_LIMIT, OK, RATE_LIMITED, RETRY_LATER, REVOKED_ENVELOPE,
                                 RUNTIME_UNSUPPORTED, TRANSIENT, V2Client, classify, next_backoff)
from domae_mcp.v2.encoding import b64d

DEVICE = "ab" * 32
BEARER = "dmk_test_" + "c" * 32
RID = {"implementation": "cpython", "python_version": "3.12.10", "magic": "cb0d0d0a",
       "artifact_kind": "marshal-code", "loader_abi": 1, "platform": "win_amd64"}
ENV = {"payload": "{}", "signing_key_id": "k", "signature": "s"}


def _client(handler, base="https://pharmsq.com/api/domae"):
    http = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
    return V2Client(BEARER, DEVICE, http=http, base=base)


def test_default_server_base(monkeypatch):
    monkeypatch.delenv("DOMAE_V2_SERVER_BASE", raising=False)
    assert client_mod.server_base() == "https://pharmsq.com/api/domae"
    monkeypatch.setenv("DOMAE_V2_SERVER_BASE", "http://127.0.0.1:9/api/domae/")
    assert client_mod.server_base() == "http://127.0.0.1:9/api/domae"


def test_headers_and_body_binding():
    seen = []

    def handler(req):
        seen.append(req)
        return httpx.Response(200, json=ENV)

    c = _client(handler)
    req = c.new_request()
    assert c.bundle(req, "cHVi", RID).kind == OK
    assert c.version(c.new_request()).kind == OK
    post, get = seen
    body = json.loads(post.content)
    assert post.method == "POST" and str(post.url) == "https://pharmsq.com/api/domae/crawlers/v2"
    assert post.headers["x-request-id"] == body["request_id"] == req.request_id_b64
    assert post.headers["x-device-id"] == body["device_id"] == DEVICE
    assert post.headers["origin"] == "https://pharmsq.com"
    assert post.headers["content-type"] == "application/json"
    assert post.headers["authorization"] == f"Bearer {BEARER}"
    assert post.headers["x-client-version"] == __version__
    assert post.headers["user-agent"] == f"MaipharmDomae/{__version__}"
    assert body == {"protocol_version": 2, "request_id": req.request_id_b64, "client_pub": "cHVi",
                    "device_id": DEVICE, "client_version": __version__, "runtime_id": RID}
    for r in seen:
        assert BEARER not in str(r.url)
    assert get.method == "GET" and str(get.url) == "https://pharmsq.com/api/domae/crawlers/v2/version"
    assert "origin" not in get.headers and get.content == b""


def test_request_id_fresh_and_canonical():
    c = _client(lambda r: httpx.Response(200, json=ENV))
    a, b = c.new_request(), c.new_request()
    assert a.request_id_b64 != b.request_id_b64
    assert len(b64d(a.request_id_b64, 16)) == 16
    assert a.device_id_hex == DEVICE


def _b(obj) -> bytes:
    return json.dumps(obj).encode()


@pytest.mark.parametrize("status,body,headers,kind,extra", [
    (200, ENV, {}, OK, {}),
    (200, {"payload": "x"}, {}, TRANSIENT, {}),
    (200, {**ENV, "extra": 1}, {}, TRANSIENT, {}),
    (200, {"payload": 1, "signing_key_id": "k", "signature": "s"}, {}, TRANSIENT, {}),
    (403, ENV, {}, REVOKED_ENVELOPE, {}),
    (403, {"error": "device_limit"}, {}, DEVICE_LIMIT, {}),
    (403, {"error": "forbidden"}, {}, AUTH_ERROR, {}),
    (401, {"error": "unknown_key"}, {}, AUTH_ERROR, {}),
    (400, {"error": "bad_body"}, {}, AUTH_ERROR, {}),
    (409, {"error": "key_not_ready"}, {}, RETRY_LATER, {}),
    (409, {"error": "conflict_retry"}, {}, RETRY_LATER, {}),
    (409, {"error": "client runtime unsupported", "supported": []}, {}, RUNTIME_UNSUPPORTED, {}),
    (409, {"error": "other"}, {}, RETRY_LATER, {}),
    (429, {"error": "daily_limit"}, {"Retry-After": "7200"}, RATE_LIMITED, {"retry_after": 7200}),
    (429, {"error": "daily_limit"}, {"Retry-After": "abc"}, RATE_LIMITED, {"retry_after": None}),
    (429, {"error": "daily_limit"}, {"Retry-After": "86401"}, RATE_LIMITED, {"retry_after": None}),
    (503, {"error": "no_release"}, {}, TRANSIENT, {"error": "no_release"}),
    (500, {"error": "internal"}, {}, TRANSIENT, {}),
    (302, {}, {"Location": "https://evil.example"}, TRANSIENT, {}),
])
def test_classify_table(status, body, headers, kind, extra):
    o = classify(status, _b(body), headers)
    assert o.kind == kind
    for k, v in extra.items():
        assert getattr(o, k) == v
    if kind in (OK, REVOKED_ENVELOPE):
        assert o.envelope == body
    else:
        assert o.envelope is None


def test_classify_non_json_and_long_error():
    assert classify(200, b"<html>", {}).kind == TRANSIENT
    assert classify(403, b"<html>", {}).kind == AUTH_ERROR
    assert len(classify(500, _b({"error": "x" * 500}), {}).error) == 64


@pytest.mark.parametrize("exc", [httpx.ConnectError("x"), httpx.ReadTimeout("x"), httpx.RemoteProtocolError("x")])
def test_transport_errors_are_transient(exc):
    def handler(req):
        raise exc

    c = _client(handler)
    assert c.version(c.new_request()).kind == TRANSIENT
    assert c.bundle(c.new_request(), "cHVi", RID).kind == TRANSIENT


def test_no_redirect_follow():
    seen = []

    def handler(req):
        seen.append(req)
        return httpx.Response(302, headers={"Location": "https://pharmsq.com/other"})

    c = _client(handler)
    assert c.version(c.new_request()).kind == TRANSIENT
    assert len(seen) == 1


def test_no_redirect_even_if_injected_client_follows():
    seen = []

    def handler(req):
        seen.append(req)
        return httpx.Response(302, headers={"Location": "https://pharmsq.com/other"})

    http = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    c = V2Client(BEARER, DEVICE, http=http, base="https://pharmsq.com/api/domae")
    assert c.version(c.new_request()).kind == TRANSIENT and len(seen) == 1


def test_bad_bearer_or_url_does_not_raise():
    c = V2Client("dmk_테스트", DEVICE, http=httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json=ENV))), base="https://pharmsq.com/api/domae")
    assert c.version(c.new_request()).kind == TRANSIENT
    c2 = _client(lambda r: httpx.Response(200, json=ENV), base="http://bad\x00host")
    assert c2.version(c2.new_request()).kind == TRANSIENT


def test_response_after_close_is_discarded():
    holder = {}

    def handler(req):
        holder["c"].close()
        return httpx.Response(200, json=ENV)

    holder["c"] = _client(handler)
    o = holder["c"].version(holder["c"].new_request())
    assert o.kind == TRANSIENT and o.cancelled is True


def test_server_error_named_cancelled_is_not_cancellation():
    o = classify(503, _b({"error": "cancelled"}), {})
    assert o.kind == TRANSIENT and o.cancelled is False


def test_repr_hides_envelope_and_device():
    o = classify(200, _b(ENV), {})
    assert "payload" not in repr(o)
    assert DEVICE not in repr(_client(lambda r: None).new_request())


def test_closed_client_is_transient_and_sends_nothing():
    seen = []
    c = _client(lambda r: (seen.append(r), httpx.Response(200, json=ENV))[1])
    c.close()
    o = c.version(c.new_request())
    assert o.kind == TRANSIENT and o.cancelled is True and seen == []


def test_backoff_growth_cap_and_retry_after():
    now = 1_800_000_000
    mid = lambda: 0.5  # noqa: E731
    assert next_backoff(0, now, None, mid) == (now + 300, 1)
    assert next_backoff(1, now, None, mid) == (now + 600, 2)
    assert next_backoff(10, now, None, mid) == (now + 21600, 11)
    assert next_backoff(500, now, None, mid) == (now + 21600, 501)
    lo, _ = next_backoff(0, now, None, lambda: 0.0)
    hi, _ = next_backoff(0, now, None, lambda: 1.0)
    assert (lo, hi) == (now + 270, now + 330)
    assert next_backoff(0, now, 7200, mid) == (now + 7200, 1)
    assert next_backoff(10, now, 60, mid) == (now + 21600, 11)
