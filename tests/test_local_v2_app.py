"""로컬 앱 v2 연결 시험 (계획 Task 8). TestClient + 실제 서비스 + 가짜 서버·시험 서명 키."""
import asyncio
import json
import os
import sys
import tempfile
import types

import pytest

if "domae_mcp.local.server" not in sys.modules:  # 모듈 수준 ConfigManager 가 실제 앱 홈에 쓰지 않게 한다
    os.environ.setdefault("MAIPHARM_DOMAE_HOME", tempfile.mkdtemp(prefix="domae-v2-test-"))

from fastapi.testclient import TestClient  # noqa: E402

from domae_mcp.core.crawlers.registry import CrawlerBlocked, CrawlerRegistry  # noqa: E402
from domae_mcp.core.services.monitor_service import MonitorService  # noqa: E402
from domae_mcp.local import v2app  # noqa: E402
from domae_mcp.local.routers import order as order_router  # noqa: E402
from domae_mcp.local.routers import search as search_router  # noqa: E402
from domae_mcp.local.routers import update as update_router  # noqa: E402
from domae_mcp.local.scheduler import LocalScheduler  # noqa: E402
from domae_mcp.local.server import app  # noqa: E402
from domae_mcp.v2.artifacts import unload_all  # noqa: E402
from domae_mcp.v2.client import V2Client  # noqa: E402
from domae_mcp.v2.encoding import key_fp_from_bearer  # noqa: E402
from domae_mcp.v2.gate import RuntimeGate  # noqa: E402
from domae_mcp.v2.keyslot import KeySlots  # noqa: E402
from domae_mcp.v2.loader import V2Loader  # noqa: E402
from domae_mcp.v2.runtime import local_runtime_id  # noqa: E402
from domae_mcp.v2.store import StateStore  # noqa: E402
from v2_fakes import (DEVICE_HEX, TEST_BEARER, TEST_BEARER_2, TEST_TRUST, FakeTime, FakeV2Server, H, T0,  # noqa: E402
                      compile_artifact, fake_artifacts, pick_protector)

FA, FB = key_fp_from_bearer(TEST_BEARER), key_fp_from_bearer(TEST_BEARER_2)
CREDS = {"login_id": "test-id", "login_pw": "test-pw"}


class Env:
    def __init__(self, root):
        self.root = root / "v2"
        self.t = FakeTime(T0)
        self.prot = pick_protector()
        self.plain = fake_artifacts()
        self.server = FakeV2Server(self.plain, now=lambda: self.t.w)
        self.jobs = []
        CrawlerRegistry.clear()
        unload_all()
        self.state = StateStore(self.root, self.prot)
        self.slots = KeySlots(self.root, self.prot)
        self.gate = RuntimeGate(self.slots, self.state, self.prot, clock=self.t.clock(), loader_factory=self.factory,
                                runner=lambda fn: self.jobs.append(fn))
        v2app.set_gate(self.gate)
        self.client = TestClient(app, client=("127.0.0.1", 50000))

    def factory(self, bearer, still_current, policy_write):
        return V2Loader(bearer, self.slots.cache_dir(key_fp_from_bearer(bearer)), self.state, self.prot,
                        client=V2Client(bearer, DEVICE_HEX, http=self.server.client(),
                                        base="https://pharmsq.com/api/domae"),
                        clock=self.t.clock(), trust=TEST_TRUST, still_current=still_current,
                        policy_write=policy_write, device_id_hex=DEVICE_HEX, runtime_id=local_runtime_id())

    def verify(self, key=TEST_BEARER):
        r = self.client.post("/api/settings/api-key/verify", json={"api_key": key})
        assert r.status_code == 200
        return r.json()

    def search(self):
        return self.client.get("/api/search", params={"keyword": "x"})


@pytest.fixture
def env(tmp_path, monkeypatch):
    e = Env(tmp_path)
    monkeypatch.setattr(search_router._config, "get_credentials",
                        lambda s: CREDS if s in ("가짜A", "가짜B", "가짜C") else {"login_id": "", "login_pw": ""})
    monkeypatch.setattr(search_router, "ALL_SUPPLIERS", None, raising=False)
    import domae_mcp.local.config as cfg
    monkeypatch.setattr(cfg, "SUPPLIERS", ["가짜A", "가짜B", "가짜C"])
    yield e
    v2app.set_gate(None)
    CrawlerRegistry.set_guard(None)
    CrawlerRegistry.clear()
    unload_all()


# ── 키 검증 = v2 초기화 ──

def test_verify_success_saves_key_after_full_load(env):
    body = env.verify()
    assert body["valid"] is True and body["crawler_count"] == 3 and body["tier"] == "local"
    assert env.slots.active() == (FA, TEST_BEARER)
    assert env.server.posts == 1


@pytest.mark.parametrize("case", ["load", "revoked", "device_limit", "rate_limited", "transient"])
def test_verify_failure_saves_nothing(env, case):
    env.verify()
    reg = dict(CrawlerRegistry._crawlers)
    cache_a = env.slots.cache_dir(FA) / "bundle-cache.json"
    cache_bytes = cache_a.read_bytes()
    if case == "load":
        env.server.plain = {**env.plain, "fake_gamma": compile_artifact("raise RuntimeError('x')", "fake_gamma")}
    elif case == "revoked":
        env.server.mode = "revoke_key"
    elif case == "device_limit":
        env.server.post_status = (403, {"error": "device_limit"}, None)
    elif case == "rate_limited":
        env.server.post_status = (429, {"error": "daily_limit"}, {"Retry-After": "3600"})
    else:
        env.server.version_status = (503, {"error": "no_release"}, None)
    body = env.verify(TEST_BEARER_2)
    assert body["valid"] is False and body["crawler_count"] == 0 and body["message"]
    assert env.slots.active() == (FA, TEST_BEARER)
    assert CrawlerRegistry._crawlers == reg
    assert cache_a.read_bytes() == cache_bytes
    assert env.search().status_code == 200


def test_verify_bad_format_no_request(env):
    body = env.verify("abc")
    assert body["valid"] is False and env.server.requests == []


def test_verify_same_key_no_post(env):
    env.verify()
    env.t.advance(60)
    assert env.verify()["valid"] is True
    assert env.server.posts == 1


def test_key_change_clears_previous_after_success(env):
    env.verify()
    assert env.verify(TEST_BEARER_2)["valid"] is True
    assert env.slots.active() == (FB, TEST_BEARER_2)
    assert not env.state.has(FA) and not env.slots.cache_dir(FA).exists()


def test_put_api_key_removed(env):
    assert env.client.put("/api/settings/api-key", json={"api_key": TEST_BEARER}).status_code in (404, 405)


# ── 진입점 차단 → 423 ──

def test_generation_raise_blocks_search_route_423(env):
    env.verify()
    env.server.device_generation = 1
    env.server.post_status = (503, {"error": "bundle_unavailable"}, None)
    env.t.advance(H)
    assert env.verify()["valid"] is False
    r = env.search()
    assert r.status_code == 423 and r.json()["reason"] == "downgrade"


def test_blocked_search_returns_423(env):
    env.verify()
    env.gate.block("key_revoked")
    r = env.search()
    assert r.status_code == 423
    assert r.json()["error"] == "crawler_blocked" and r.json()["reason"] == "key_revoked" and r.json()["message"]


def test_blocked_order_returns_423(env, monkeypatch):
    env.verify()
    env.gate.block("device_revoked")
    monkeypatch.setattr(order_router._config, "get_credentials", lambda s: CREDS)
    r = env.client.post("/api/orders", json={"supplier": "가짜A", "product_id": "p", "product_name": "n",
                                             "quantity": 1})
    assert r.status_code == 423 and r.json()["reason"] == "device_revoked"


def test_block_during_search_propagates(env):
    env.verify()
    calls = {"n": 0}

    def guard():
        calls["n"] += 1
        if calls["n"] > 2:  # require()·list_all 은 통과, 작업 스레드의 get 에서 막힌다
            raise CrawlerBlocked("offline_deadline")

    CrawlerRegistry.set_guard(guard)
    r = env.search()
    assert r.status_code == 423 and r.json()["reason"] == "offline_deadline"


def test_credentials_test_blocked_returns_423(env, monkeypatch):
    env.verify()
    env.gate.block("key_revoked")
    from domae_mcp.local.routers import settings as settings_router
    monkeypatch.setattr(settings_router._config, "get_credentials", lambda s: CREDS)
    r = env.client.post("/api/settings/credentials/test", json={"supplier": "가짜A"})
    assert r.status_code == 423 and r.json()["reason"] == "key_revoked"


def test_monitor_service_loop_skips_blocked_cycle(env, monkeypatch):
    env.verify()
    env.gate.block("key_revoked")
    svc = MonitorService(db_session_factory=lambda: None, credentials={}, telegram_service=None)
    monkeypatch.setattr(svc, "_get_current_interval", lambda: (svc._stop_event.set(), 0)[1])
    svc._loop()  # 예외 없이 회차를 건너뛴다
    assert svc.last_run is None


def test_monitor_cycle_skipped_when_blocked(env, monkeypatch):
    env.verify()
    env.gate.block("key_revoked")
    opened = {"n": 0}
    svc = MonitorService(db_session_factory=lambda: opened.__setitem__("n", 1), credentials={}, telegram_service=None)
    with pytest.raises(CrawlerBlocked):
        svc.run_cycle()
    assert opened["n"] == 0
    sched = LocalScheduler(svc, db_session_factory=lambda: None)
    monkeypatch.setattr(sched, "_get_current_interval_seconds", lambda: (sched._stop_event.set(), 0)[1])
    sched._loop()  # 예외 없이 그 회차를 건너뛴다
    assert sched.last_run is None


def test_mcp_tool_blocked_message(env, monkeypatch):
    fake_server = types.ModuleType("mcp.server")

    class FakeServer:
        def __init__(self, *a, **k):
            pass

        def list_tools(self):
            return lambda f: f

        def call_tool(self):
            return lambda f: f

    fake_server.Server = FakeServer
    fake_stdio = types.ModuleType("mcp.server.stdio")
    fake_stdio.stdio_server = None
    fake_types = types.ModuleType("mcp.types")

    class _T:
        def __init__(self, **k):
            self.__dict__.update(k)

    fake_types.Tool = fake_types.TextContent = _T
    monkeypatch.setitem(sys.modules, "mcp", types.ModuleType("mcp"))
    monkeypatch.setitem(sys.modules, "mcp.server", fake_server)
    monkeypatch.setitem(sys.modules, "mcp.server.stdio", fake_stdio)
    monkeypatch.setitem(sys.modules, "mcp.types", fake_types)
    monkeypatch.delitem(sys.modules, "domae_mcp.local.mcp_server", raising=False)
    import domae_mcp.local.mcp_server as m
    from domae_mcp.core.services.search_service import SearchService
    monkeypatch.setattr(m, "_search_service", SearchService())
    monkeypatch.setattr(m, "_get_all_credentials_dict", lambda: {})
    env.verify()
    env.gate.block("key_revoked")
    out = asyncio.run(m.call_tool("search_inventory", {"keyword": "x"}))
    body = json.loads(out[0].text)
    assert body["error"] == "crawler_blocked" and body["reason"] == "key_revoked" and body["message"]
    monkeypatch.setattr(m, "_config", types.SimpleNamespace(get_credentials=lambda s: CREDS))
    out = asyncio.run(m.call_tool("test_credential", {"supplier": "가짜A"}))
    assert json.loads(out[0].text)["error"] == "crawler_blocked"
    monkeypatch.delitem(sys.modules, "domae_mcp.local.mcp_server", raising=False)


def test_settings_and_update_available_when_blocked(env, monkeypatch):
    env.verify()
    env.gate.block("key_revoked")
    s = env.client.get("/api/settings/setup-status")
    assert s.status_code == 200
    st = s.json()
    assert st["crawler_state"]["state"] == "blocked" and st["crawler_state"]["reason"] == "key_revoked"
    assert "dmk_test" not in json.dumps(st)
    assert env.client.get("/api/settings/credentials").status_code == 200

    class R:
        def raise_for_status(self):
            pass

        def json(self):
            return {"tag_name": "v1.4.1", "html_url": "https://example.invalid/r"}

    monkeypatch.setattr(update_router.requests, "get", lambda *a, **k: R())
    u = env.client.get("/api/update-check")
    assert u.status_code == 200 and u.json()["current_version"] == "1.4.0" and u.json()["update_available"] is True


def test_same_key_reverify_revoked_blocks_gate(env):
    env.verify()
    env.server.mode = "revoke_key"
    env.t.advance(60)
    body = env.verify()
    assert body["valid"] is False and "비활성" in body["message"]
    r = env.search()
    assert r.status_code == 423 and r.json()["reason"] == "key_revoked"


def test_startup_without_key_serves_settings(env):
    env.gate.start()
    st = env.client.get("/api/settings/setup-status").json()
    assert st["api_key_set"] is False and st["crawler_state"]["state"] == "no_key" and st["crawler_count"] == 0
    r = env.search()
    assert r.status_code == 423 and r.json()["reason"] == "not_loaded"


def test_lifespan_starts_gate_without_v1_or_production_calls(env, monkeypatch):
    import httpx
    import requests
    bad = []
    real_send = httpx.Client.send

    def guarded_send(self, request, *a, **k):
        if request.url.host != "pharmsq.com" or not request.url.path.startswith("/api/domae/crawlers/v2"):
            bad.append(str(request.url.path))
            raise AssertionError("unexpected request")
        return real_send(self, request, *a, **k)

    def no_requests(*a, **k):
        bad.append("requests")
        raise AssertionError("unexpected requests call")

    monkeypatch.setattr(httpx.Client, "send", guarded_send)
    monkeypatch.setattr(requests.Session, "request", no_requests)
    env.slots.stage(TEST_BEARER)
    env.slots.commit(FA)
    with TestClient(app, client=("127.0.0.1", 50000)) as c:  # lifespan → v2app.start_background
        import time as _t
        for _ in range(100):
            if env.gate.status()["state"] != "no_key" and env.server.requests:
                break
            _t.sleep(0.05)
        for _ in range(100):
            if env.gate.status()["state"] == "ready":
                break
            _t.sleep(0.05)
        assert env.gate.status()["state"] == "ready"
        assert c.get("/api/search", params={"keyword": "x"}).status_code == 200
    assert bad == []


def test_no_v1_endpoints_called(env):
    env.verify()
    env.search()
    env.t.advance(72 * H)
    env.search()
    for fn in env.jobs:
        fn()
    env.verify(TEST_BEARER_2)
    paths = {r.url.path for r in env.server.requests}
    assert paths and all(p in ("/api/domae/crawlers/v2", "/api/domae/crawlers/v2/version") for p in paths)
    assert all(r.url.host == "pharmsq.com" for r in env.server.requests)


def test_order_not_started_if_revoked_during_login(env):
    from domae_mcp.core.crawlers.base import BaseCrawler, OrderResult
    from domae_mcp.core.services.order_service import OrderService
    env.verify()
    calls = {"order": 0}
    gate = env.gate

    class Slow(BaseCrawler):
        SUPPLIER_NAME = "가짜A"

        def login(self, i, p):
            return True

        def ensure_login(self, i, p):
            gate.block("key_revoked")  # 로그인 대기 중 철회됨

        def search(self, k):
            return []

        def order(self, product_id, quantity):
            calls["order"] += 1
            return OrderResult(success=True)

    CrawlerRegistry.register_all({"가짜A": Slow})

    class DB:
        def add(self, o):
            raise AssertionError("주문 기록 없음")

        def commit(self):
            pass

    with pytest.raises(CrawlerBlocked):
        OrderService().place_order("가짜A", "p", "", 1, CREDS, DB())
    assert calls["order"] == 0


def test_masked_key_reverifies_active_key(env):
    env.verify()
    st = env.client.get("/api/settings/setup-status").json()
    env.t.advance(60)
    body = env.verify(st["api_key_prefix"])
    assert body["valid"] is True and env.server.posts == 1
    assert env.slots.active() == (FA, TEST_BEARER)
