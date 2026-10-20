"""로컬 웹 화면 경로(/settings 등)를 새로고침해도 React 앱이 열리는지(G7 실기에서 404 발견)"""

import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.exceptions import HTTPException

from domae_mcp.local.spa import SPAStaticFiles


def _client(tmp_path):
    (tmp_path / "index.html").write_text("<div id=root></div>", encoding="utf-8")
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "app.js").write_text("console.log(1)", encoding="utf-8")
    app = FastAPI()

    @app.get("/api/ping")
    def ping():
        return {"ok": True}

    app.mount("/", SPAStaticFiles(directory=str(tmp_path), html=True), name="static")
    return TestClient(app)


def test_client_route_refresh_serves_index(tmp_path):
    c = _client(tmp_path)
    for path in ("/settings", "/setup", "/search", "/settings/accounts"):
        r = c.get(path)
        assert r.status_code == 200, path
        assert "id=root" in r.text


def test_existing_files_and_api_unchanged(tmp_path):
    c = _client(tmp_path)
    assert c.get("/assets/app.js").text == "console.log(1)"
    assert c.get("/api/ping").json() == {"ok": True}


def test_missing_asset_and_unknown_api_stay_404(tmp_path):
    c = _client(tmp_path)
    assert c.get("/assets/missing.js").status_code == 404
    assert c.get("/api/unknown").status_code == 404
    assert "id=root" not in c.get("/api/unknown").text


def test_windows_separator_api_path_stays_404(tmp_path):
    # Windows 에서 StaticFiles 는 경로를 os.sep('\\')로 넘긴다(pico 재빌드 전 발견)
    (tmp_path / "index.html").write_text("<div id=root></div>", encoding="utf-8")
    app = SPAStaticFiles(directory=str(tmp_path), html=True)
    scope = {"type": "http", "method": "GET", "path": "/", "headers": []}
    for p in ("api\\unknown", "assets\\missing.js"):
        with pytest.raises(HTTPException) as e:
            asyncio.run(app.get_response(p, scope))
        assert e.value.status_code == 404, p
