"""도매 v2 시험 전용 로컬 가짜 서버 (B-4 Task 11 G-C). 127.0.0.1 에만 묶는다. 운영 서버가 아니다.

B-3 계약(헤더 검사·버전 확인·번들 POST·철회·429·503)을 흉내 낸다. 서명 키는 실행 때마다 새로 만들고
공개키만 --trust-out 에 쓴다. 산출물 디렉터리(build_crawler_artifacts.py 출력)는 인자로만 받는다.

사용: python scripts/v2_fake_server.py --artifacts <dir> --trust-out <file> [--port 18765] [--log <file>]
제어(같은 호스트에서만):
  POST /__fake/mode {"mode": "normal" | "503" | "429" | "revoke"}   429 는 새 게시(release_seq+1)를 알리고 POST 에 429 를 준다
  GET  /__fake/stats → {"mode", "release_seq", "posts", "gets", "requests": [[method, path, status], ...]}
요청 본문·키 원문은 기록하지 않는다(경로·상태만).
"""
import argparse
import base64
import hashlib
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey  # noqa: E402
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, PublicFormat  # noqa: E402

from domae_mcp.v2.bundle_crypto import (PROTOCOL_VERSION, BundleContext, derive_session_key,  # noqa: E402
                                        encrypt_module, x25519_shared)
from domae_mcp.v2.encoding import EncodingError, b64d, encode_runtime_id, hexd  # noqa: E402
from domae_mcp.v2.envelope import DOMAIN_BUNDLE, DOMAIN_RELEASE, DOMAIN_REVOCATION, signed_digest  # noqa: E402

PREFIX = "/api/domae/crawlers/v2"
BUNDLE_LIFETIME_S = 72 * 3600
OFFLINE_GRACE_S = 72 * 3600
SHORT_VALIDITY_S = 300


def b64(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")


class FakeState:
    def __init__(self, artifacts: Path):
        self.dir = artifacts
        self.build = json.loads((artifacts / "build.json").read_text(encoding="ascii"))
        self.signing = Ed25519PrivateKey.generate()
        pub = self.signing.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        self.pub_b64 = b64(pub)
        self.key_id = "ed25519-" + hashlib.sha256(pub).hexdigest()[:16]
        self.lock = threading.Lock()
        self.mode = "normal"
        self.release_seq = 1
        self.devices: dict[tuple[str, str], int] = {}
        self.requests: list[list] = []

    def sign(self, domain: str, payload: dict) -> dict:
        s = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        return {"payload": s, "signing_key_id": self.key_id,
                "signature": b64(self.signing.sign(signed_digest(domain, self.key_id, s)))}

    def runtime(self, rid: dict):
        want = encode_runtime_id(rid)
        for rt in self.build["runtimes"]:
            if encode_runtime_id(rt["runtime_id"]) == want:
                return rt
        return None

    def manifest(self, rt: dict) -> dict:
        arts = {n: {"magic": rt["runtime_id"]["magic"], "python_version": rt["runtime_id"]["python_version"],
                    "sha256": b64(bytes.fromhex(a["sha256"])), "size": a["size"]}
                for n, a in rt["artifacts"].items()}
        return {"artifacts": arts, "base_abi": self.build["base_abi"], "bundle_hash": self.bundle_hash(),
                "cart_abi": self.build["cart_abi"], "min_client_version": self.build["min_client_version"],
                "min_protocol_version": 1, "min_release_seq": 1, "required_modules": list(self.build["modules"])}

    def bundle_hash(self) -> str:
        # 시험용: 게시가 바뀌면(429 모드) 다른 hash 를 쓴다
        if self.release_seq == 1:
            return self.build["bundle_hash"]
        return hashlib.sha256(f"{self.build['bundle_hash']}:{self.release_seq}".encode()).hexdigest()


def make_handler(st: FakeState, log_path: Path | None):
    class H(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def _send(self, status: int, body: dict, headers: dict | None = None):
            data = json.dumps(body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)
            with st.lock:
                st.requests.append([self.command, self.path.split("?")[0], status])
            if log_path:
                with open(log_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps({"t": int(time.time()), "m": self.command, "p": self.path.split("?")[0],
                                        "s": status}) + "\n")

        def _ident(self):
            auth = self.headers.get("Authorization", "")
            if not auth.startswith("Bearer dmk_") or len(auth) > 200:
                return None, (401, {"error": "unknown_key"})
            rid, did = self.headers.get("X-Request-Id", ""), self.headers.get("X-Device-Id", "")
            try:
                b64d(rid, 16)
            except EncodingError:
                return None, (400, {"error": "bad_request_id"})
            try:
                hexd(did, 32)
            except EncodingError:
                return None, (400, {"error": "bad_device_id"})
            return (hashlib.sha256(auth[7:].encode("utf-8")).digest(), rid, did), None

        def do_GET(self):
            path = self.path.split("?")[0]
            if path == "/__fake/stats":
                with st.lock:
                    body = {"mode": st.mode, "release_seq": st.release_seq,
                            "posts": sum(1 for r in st.requests if r[0] == "POST" and r[1] == PREFIX),
                            "gets": sum(1 for r in st.requests if r[0] == "GET" and r[1] == PREFIX + "/version"),
                            "requests": list(st.requests)}
                data = json.dumps(body).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            if path == PREFIX + "/signing-key":
                return self._send(200, {"signing_key_id": st.key_id, "public_key": st.pub_b64})
            if path != PREFIX + "/version":
                return self._send(404, {"error": "not_found"})
            ident, err = self._ident()
            if err:
                return self._send(*err)
            fp, rid, did = ident
            now = int(time.time())
            with st.lock:
                mode, seq = st.mode, st.release_seq
                gen = st.devices.get((fp.hex(), did))
                bundle_hash = st.bundle_hash()
            if mode == "revoke":
                return self._revoke(fp, rid, did, now)
            if mode == "503":
                return self._send(503, {"error": "no_release"})
            p = {"protocol_version": PROTOCOL_VERSION, "request_id": rid, "key_fp": b64(fp), "device_id": did,
                 "bundle_hash": bundle_hash, "release_seq": seq, "min_release_seq": 1, "min_protocol_version": 1,
                 "issued_at": now, "valid_until": now + SHORT_VALIDITY_S}
            if gen is not None:
                p["device_generation"] = gen
            return self._send(200, st.sign(DOMAIN_RELEASE, p))

        def _revoke(self, fp, rid, did, now):
            p = {"error": "key_revoked", "scope": "key", "request_id": rid, "key_fp": b64(fp), "device_id": did,
                 "revoked_at": now, "issued_at": now, "valid_until": now + SHORT_VALIDITY_S}
            return self._send(403, st.sign(DOMAIN_REVOCATION, p))

        def do_POST(self):
            path = self.path.split("?")[0]
            length = int(self.headers.get("Content-Length", "0") or 0)
            raw = self.rfile.read(min(length, 1 << 20)) if length else b""
            if path == "/__fake/mode":
                try:
                    mode = json.loads(raw)["mode"]
                except (ValueError, KeyError, TypeError):
                    return self._send(400, {"error": "bad_mode"})
                if mode not in ("normal", "503", "429", "revoke"):
                    return self._send(400, {"error": "bad_mode"})
                with st.lock:
                    if mode == "429" and st.mode != "429":
                        st.release_seq += 1  # 새 게시를 알린다
                    st.mode = mode
                return self._send(200, {"mode": mode})
            if path != PREFIX:
                return self._send(404, {"error": "not_found"})
            ident, err = self._ident()
            if err:
                return self._send(*err)
            fp, rid, did = ident
            if self.headers.get("Origin") != "https://pharmsq.com":
                return self._send(403, {"error": "origin"})
            try:
                body = json.loads(raw)
                if body.get("protocol_version") != PROTOCOL_VERSION or body.get("request_id") != rid \
                        or body.get("device_id") != did:
                    raise ValueError("binding")
                client_pub = b64d(body.get("client_pub"), 32)
                rid_obj = body.get("runtime_id")
                encode_runtime_id(rid_obj)
            except (ValueError, EncodingError, TypeError, AttributeError):
                return self._send(400, {"error": "bad_body"})
            now = int(time.time())
            with st.lock:
                mode, seq = st.mode, st.release_seq
                key = (fp.hex(), did)
                gen = st.devices.setdefault(key, 0)
                bundle_hash = st.bundle_hash()
            if mode == "revoke":
                return self._revoke(fp, rid, did, now)
            if mode == "503":
                return self._send(503, {"error": "no_release"})
            if mode == "429":
                return self._send(429, {"error": "daily_limit"}, {"Retry-After": "7200"})
            rt = st.runtime(rid_obj)
            if rt is None:
                return self._send(409, {"error": "client runtime unsupported"})
            plain = {n: (st.dir / a["file"]).read_bytes() for n, a in rt["artifacts"].items()}
            manifest = st.manifest(rt)
            server = X25519PrivateKey.generate()
            server_pub = server.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
            salt = os.urandom(32)
            ctx = BundleContext(PROTOCOL_VERSION, b64d(rid, 16), client_pub, server_pub, fp, bytes.fromhex(did),
                                bundle_hash, seq, rt["runtime_id"])
            shared = x25519_shared(server.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption()), client_pub)
            session = derive_session_key(shared, salt, ctx)
            modules = {n: encrypt_module(session, ctx, n, plain[n], os.urandom(12)) for n in sorted(plain)}
            p = {"protocol_version": PROTOCOL_VERSION, "request_id": rid, "client_pub": body["client_pub"],
                 "server_pub": b64(server_pub), "salt": b64(salt), "issued_at": now,
                 "expires_at": now + BUNDLE_LIFETIME_S, "offline_deadline": now + BUNDLE_LIFETIME_S + OFFLINE_GRACE_S,
                 "key_fp": b64(fp), "device_id": did, "device_generation": gen, "bundle_hash": bundle_hash,
                 "release_seq": seq, "runtime_id": rt["runtime_id"], "manifest": manifest, "modules": modules}
            return self._send(200, st.sign(DOMAIN_BUNDLE, p))

    return H


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--artifacts", type=Path, required=True)
    ap.add_argument("--trust-out", type=Path, required=True)
    ap.add_argument("--port", type=int, default=18765)
    ap.add_argument("--log", type=Path)
    a = ap.parse_args(argv)
    st = FakeState(a.artifacts)
    a.trust_out.write_text(json.dumps({"signing_key_id": st.key_id, "public_key": st.pub_b64}), encoding="utf-8")
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), make_handler(st, a.log))
    print(f"fake v2 server on 127.0.0.1:{a.port} modules={len(st.build['modules'])} key_id={st.key_id}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
