"""FastAPI 웹서버: 0.0.0.0 바인딩, 로컬 네트워크 전용"""

import asyncio
import ipaddress
import logging
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.responses import JSONResponse

from domae_mcp.local.config import ConfigManager
from domae_mcp.local.database import init_db
from domae_mcp.core.crawlers.loader import CrawlerLoader
from domae_mcp.core.crawlers.registry import CrawlerRegistry

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """앱 시작/종료 이벤트"""
    config = ConfigManager()
    init_db(config)

    # 크롤러 서버 로드 (동기 loader를 asyncio.to_thread로 래핑)
    api_key = config.get_api_key()
    loader = None
    if api_key:
        try:
            loader = CrawlerLoader(config.base_dir, api_key)
            crawlers = await asyncio.to_thread(loader.load)
            CrawlerRegistry.register_all(crawlers)
        except RuntimeError as e:
            # 크롤러 로드 실패해도 서버는 시작 (설정 페이지 접근 필요)
            logger.warning("크롤러 로드 실패: %s", e)
    else:
        logger.warning("API 키 미설정 — 크롤러 없이 시작. 설정 페이지에서 API 키를 등록하세요.")

    app.state.config = config
    app.state.crawler_loader = loader

    yield
    # 종료: 정리 작업


app = FastAPI(
    title="maipharm-domae-mcp",
    description="의약품 도매 통합 검색/주문 로컬 서버",
    version="1.0.0",
    lifespan=lifespan,
)

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
