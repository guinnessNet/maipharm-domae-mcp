"""도매 v2 시험 전용 도우미. TEST ONLY — 실제 크롤러·운영 키와 무관한 합성 값만 둔다."""
import base64
import hashlib
import importlib.util
import marshal
import platform

# ── 합성 크롤러 산출물 (Task 4·6) ──

FAKE_SOURCES = {
    "fake_alpha": '''
from domae_mcp.core.crawlers.base import BaseCrawler, SearchResult, OrderResult


class FakeAlphaCrawler(BaseCrawler):
    SUPPLIER_NAME = "가짜A"

    def login(self, login_id, login_pw):
        return True

    def search(self, keyword):
        return [SearchResult(product_name=keyword, supplier=self.SUPPLIER_NAME)]
''',
    "fake_beta": '''
from domae_mcp.core.crawlers.base import BaseCrawler, SearchResult, OrderResult
from domae_mcp.core.crawlers.cart_snapshot import __dict__ as _cart_abi
from inspect import signature

_ORDER_SIG = signature(OrderResult)


class FakeBetaCrawler(BaseCrawler):
    SUPPLIER_NAME = "가짜B"

    def login(self, login_id, login_pw):
        return True

    def search(self, keyword):
        return []
''',
    "fake_gamma": '''
from domae_mcp.core.crawlers.base import BaseCrawler, SearchResult, OrderResult


class FakeGammaCrawler(BaseCrawler):
    SUPPLIER_NAME = "가짜C"

    def login(self, login_id, login_pw):
        return False

    def search(self, keyword):
        return []
''',
}


def compile_artifact(src: str, name: str) -> bytes:
    return marshal.dumps(compile(src, name, "exec", optimize=2))


def fake_artifacts(sources: dict[str, str] | None = None) -> dict[str, bytes]:
    return {n: compile_artifact(s, n) for n, s in (sources or FAKE_SOURCES).items()}


def b64(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")


def make_manifest(plain: dict[str, bytes], bundle_hash: str = "ab" * 32, min_release_seq: int = 1,
                  min_protocol_version: int = 1, min_client_version: str = "1.4.0", base_abi: int = 1,
                  cart_abi: int = 1) -> dict:
    return {
        "artifacts": {n: {"magic": importlib.util.MAGIC_NUMBER.hex(), "python_version": platform.python_version(),
                          "sha256": b64(hashlib.sha256(b).digest()), "size": len(b)} for n, b in plain.items()},
        "base_abi": base_abi,
        "bundle_hash": bundle_hash,
        "cart_abi": cart_abi,
        "min_client_version": min_client_version,
        "min_protocol_version": min_protocol_version,
        "min_release_seq": min_release_seq,
        "required_modules": sorted(plain),
    }


# ── 시험 서명 키·envelope 조립 (Task 5~8). server responses.ts 와 같은 필드·수명 ──

import json  # noqa: E402
import os  # noqa: E402
import time  # noqa: E402

import httpx  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey  # noqa: E402
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, PublicFormat  # noqa: E402

from domae_mcp.v2.bundle_crypto import (PROTOCOL_VERSION, BundleContext, derive_session_key,  # noqa: E402
                                        encrypt_module, x25519_shared)
from domae_mcp.v2.encoding import key_fp_from_bearer  # noqa: E402
from domae_mcp.v2.envelope import DOMAIN_BUNDLE, DOMAIN_RELEASE, DOMAIN_REVOCATION, signed_digest  # noqa: E402

# TEST ONLY: B-1 벡터 생성기·운영 키와 다른 시험 전용 서명 키
TEST_KEY_ID = "ed25519-test-only"
_TEST_SIGNING = Ed25519PrivateKey.from_private_bytes(hashlib.sha256(b"domae-v2-b4 TEST ONLY signing key").digest())
TEST_PUB = _TEST_SIGNING.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
TEST_TRUST = {TEST_KEY_ID: TEST_PUB}
OTHER_KEY_ID = "ed25519-test-other"
_OTHER_SIGNING = Ed25519PrivateKey.from_private_bytes(hashlib.sha256(b"domae-v2-b4 TEST ONLY other key").digest())

BUNDLE_LIFETIME_S = 72 * 3600
OFFLINE_GRACE_S = 72 * 3600
SHORT_VALIDITY_S = 300
TEST_BEARER = "dmk_test_" + "a" * 32
TEST_BEARER_2 = "dmk_test_" + "b" * 32


def sign(domain: str, payload, key_id: str = TEST_KEY_ID, signer=None) -> dict:
    s = payload if isinstance(payload, str) else json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    signer = signer or (_OTHER_SIGNING if key_id == OTHER_KEY_ID else _TEST_SIGNING)
    return {"payload": s, "signing_key_id": key_id, "signature": b64(signer.sign(signed_digest(domain, key_id, s)))}


def make_release_envelope(request_id_b64: str, key_fp: bytes, device_id_hex: str, release_seq: int,
                          bundle_hash: str, now: int, min_release_seq: int = 1, min_protocol_version: int = 1,
                          device_generation: int | None = None, **sign_kw) -> dict:
    p = {"protocol_version": PROTOCOL_VERSION, "request_id": request_id_b64, "key_fp": b64(key_fp),
         "device_id": device_id_hex, "bundle_hash": bundle_hash, "release_seq": release_seq,
         "min_release_seq": min_release_seq, "min_protocol_version": min_protocol_version,
         "issued_at": now, "valid_until": now + SHORT_VALIDITY_S}
    if device_generation is not None:
        p["device_generation"] = device_generation
    return sign(DOMAIN_RELEASE, p, **sign_kw)


def make_revocation_envelope(scope: str, request_id_b64: str, key_fp: bytes, device_id_hex: str, now: int,
                             revoked_at: int | None = None, device_generation: int | None = None, **sign_kw) -> dict:
    p = {"error": "key_revoked" if scope == "key" else "device_revoked", "scope": scope,
         "request_id": request_id_b64, "key_fp": b64(key_fp), "device_id": device_id_hex,
         "revoked_at": now if revoked_at is None else revoked_at, "issued_at": now, "valid_until": now + SHORT_VALIDITY_S}
    if scope == "device":
        p["device_generation"] = 0 if device_generation is None else device_generation
    return sign(DOMAIN_REVOCATION, p, **sign_kw)


def make_bundle_envelope(plain: dict[str, bytes], manifest: dict, request_id_b64: str, client_pub_b64: str,
                         key_fp: bytes, device_id_hex: str, runtime_id: dict, release_seq: int, now: int,
                         device_generation: int = 0, mutate=None, encrypt_plain: dict | None = None,
                         **sign_kw) -> dict:
    """mutate(payload) 로 서명 전에 payload 를 바꿀 수 있다. encrypt_plain 을 주면 그 바이트를 암호화한다(시험용)."""
    server = X25519PrivateKey.generate()
    server_pub = server.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    salt = os.urandom(32)
    client_pub = base64.b64decode(client_pub_b64)
    ctx = BundleContext(PROTOCOL_VERSION, base64.b64decode(request_id_b64), client_pub, server_pub, key_fp,
                        bytes.fromhex(device_id_hex), manifest["bundle_hash"], release_seq, runtime_id)
    shared = x25519_shared(server.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption()), client_pub)
    key = derive_session_key(shared, salt, ctx)
    src = encrypt_plain or plain
    modules = {n: encrypt_module(key, ctx, n, src[n], os.urandom(12)) for n in sorted(src)}
    p = {"protocol_version": PROTOCOL_VERSION, "request_id": request_id_b64, "client_pub": client_pub_b64,
         "server_pub": b64(server_pub), "salt": b64(salt), "issued_at": now, "expires_at": now + BUNDLE_LIFETIME_S,
         "offline_deadline": now + BUNDLE_LIFETIME_S + OFFLINE_GRACE_S, "key_fp": b64(key_fp),
         "device_id": device_id_hex, "device_generation": device_generation, "bundle_hash": manifest["bundle_hash"],
         "release_seq": release_seq, "runtime_id": runtime_id, "manifest": manifest, "modules": modules}
    if mutate:
        mutate(p)
    return sign(DOMAIN_BUNDLE, p, **sign_kw)


class FakeV2Server:
    """httpx.MockTransport 용 가짜 B-3 서버(시험 전용). 헤더를 검사하고 요청을 기록한다.

    mode: normal | revoke_key | revoke_device.
    version_status / post_status 를 (status, body, headers) 로 주면 그 응답을 돌려준다(지울 때까지 계속).
    post_mutate(payload) 는 서명 전에 번들 payload 를 바꾼다.
    """

    def __init__(self, plain: dict[str, bytes], release_seq: int = 1, bundle_hash: str = "ab" * 32, now=None,
                 runtime_id: dict | None = None):
        self.plain = plain
        self.release_seq = release_seq
        self.bundle_hash = bundle_hash
        self.min_release_seq = 1
        self.min_protocol_version = 1
        self.device_generation: int | None = None  # POST 로 기기 행이 생기면 generation_on_register
        self.generation_on_register = 0
        self.now = now or (lambda: int(time.time()))
        self.runtime_id = runtime_id
        self.mode = "normal"
        self.version_status = None
        self.post_status = None
        self.post_mutate = None
        self.post_encrypt_plain = None
        self.post_sign_kw: dict = {}
        self.manifest_kw: dict = {}
        self.requests: list[httpx.Request] = []
        self.on_request = None

    @property
    def posts(self) -> int:
        return sum(1 for r in self.requests if r.method == "POST")

    @property
    def gets(self) -> int:
        return sum(1 for r in self.requests if r.method == "GET")

    def manifest(self) -> dict:
        kw = {"min_release_seq": self.min_release_seq, "min_protocol_version": self.min_protocol_version}
        kw.update(self.manifest_kw)
        return make_manifest(self.plain, bundle_hash=self.bundle_hash, **kw)

    def publish(self, plain: dict[str, bytes] | None = None, bundle_hash: str | None = None):
        if plain is not None:
            self.plain = plain
        self.release_seq += 1
        self.bundle_hash = bundle_hash or hashlib.sha256(str(self.release_seq).encode()).hexdigest()

    def _ident(self, req: httpx.Request):
        auth = req.headers.get("authorization", "")
        assert auth.startswith("Bearer dmk_"), "bearer"
        assert "dmk_" not in str(req.url), "bearer in url"
        return key_fp_from_bearer(auth[7:]), req.headers["x-request-id"], req.headers["x-device-id"]

    @staticmethod
    def _resp(spec):
        status, body, headers = spec
        return httpx.Response(status, json=body, headers=headers or {})

    def handler(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        if self.on_request:
            self.on_request(req)
        path = req.url.path
        if not path.startswith("/api/domae/crawlers/v2"):
            raise AssertionError(f"unexpected path {path}")
        fp, rid, did = self._ident(req)
        now = self.now()
        if self.mode in ("revoke_key", "revoke_device"):
            scope = self.mode.split("_")[1]
            gen = self.device_generation if self.device_generation is not None else 0
            return httpx.Response(403, json=make_revocation_envelope(scope, rid, fp, did, now, device_generation=gen))
        if req.method == "GET" and path.endswith("/crawlers/v2/version"):
            if self.version_status:
                return self._resp(self.version_status)
            return httpx.Response(200, json=make_release_envelope(
                rid, fp, did, self.release_seq, self.bundle_hash, now, self.min_release_seq,
                self.min_protocol_version, self.device_generation))
        if req.method == "POST" and path.endswith("/crawlers/v2"):
            assert req.headers.get("origin") == "https://pharmsq.com"
            body = json.loads(req.content)
            assert body["request_id"] == rid and body["device_id"] == did
            if self.post_status:
                return self._resp(self.post_status)
            if self.device_generation is None:
                self.device_generation = self.generation_on_register
            rt = self.runtime_id or body["runtime_id"]
            return httpx.Response(200, json=make_bundle_envelope(
                self.plain, self.manifest(), rid, body["client_pub"], fp, did, rt, self.release_seq, now,
                self.device_generation, mutate=self.post_mutate, encrypt_plain=self.post_encrypt_plain,
                **self.post_sign_kw))
        raise AssertionError(f"unexpected {req.method} {path}")

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    def client(self) -> httpx.Client:
        return httpx.Client(transport=self.transport(), follow_redirects=False)
