"""FastAPI 웹서버: 0.0.0.0 바인딩, 로컬 네트워크 전용"""

import ipaddress
import logging
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.responses import JSONResponse

from domae_mcp import __version__
from domae_mcp.core.crawlers.registry import CrawlerBlocked
from domae_mcp.local import v2app
from domae_mcp.local.config import ConfigManager
from domae_mcp.local.database import init_db

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """앱 시작/종료 이벤트"""
    config = ConfigManager()
    init_db(config)

    # 도매 v2 시작 흐름(캐시 → 버전 확인 → 필요하면 다운로드). 실패해도 서버는 뜬다(설정 화면 필요)
    try:
        v2app.start_background()
    except Exception:
        logger.exception("v2 게이트 준비 실패 — 크롤러 없이 시작")

    app.state.config = config

    yield
    # 종료: 정리 작업


app = FastAPI(
    title="maipharm-domae-mcp",
    description="의약품 도매 통합 검색/주문 로컬 서버",
    version=__version__,
    lifespan=lifespan,
)


@app.exception_handler(CrawlerBlocked)
async def crawler_blocked_handler(request: Request, exc: CrawlerBlocked):
    return JSONResponse(status_code=423, content=v2app.blocked_body(exc))


@app.middleware("http")
async def local_network_only(request: Request, call_next):
    client_ip = request.client.host if request.client else None
    if client_ip:
        try:
            addr = ipaddress.ip_address(client_ip)
            if not (addr.is_loopback or addr.is_private):
                return JSONResponse(status_code=403, content={"detail": "로컬 네트워크 접근만 허용됩니다"})
        except ValueError:
            return JSONResponse(status_code=403, content={"detail": "잘못된 접근입니다"})
    return await call_next(request)

app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1|192\.168\.\d+\.\d+|10\.\d+\.\d+\.\d+|172\.(1[6-9]|2\d|3[01])\.\d+\.\d+)(:\d+)?$",
    allow_methods=["*"],
    allow_headers=["*"],
)

# 라우터 마운트
from domae_mcp.local.routers import search, order, urgent, products, settings, monitor, supplier_request, update

app.include_router(search.router, prefix="/api")
app.include_router(order.router, prefix="/api")
app.include_router(urgent.router, prefix="/api")
app.include_router(products.router, prefix="/api")
app.include_router(settings.router, prefix="/api")
app.include_router(monitor.router, prefix="/api")
app.include_router(supplier_request.router, prefix="/api")
app.include_router(update.router, prefix="/api")

# 정적 파일 서빙 (React 빌드 결과물)
static_dir = Path(__file__).parent.parent / "static"
if static_dir.is_dir():
    app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="static")


def run_web_server(port: int = 5900) -> None:
    """웹서버 실행 (0.0.0.0 바인딩 — 로컬 네트워크 허용)"""
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=port,
        log_level="info",
    )
