"""FastAPI 웹서버: 127.0.0.1 바인딩, 로컬 전용"""

from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from domae_mcp.local.config import ConfigManager
from domae_mcp.local.database import init_db


@asynccontextmanager
async def lifespan(app: FastAPI):
    """앱 시작/종료 이벤트"""
    # 시작: DB 초기화
    config = ConfigManager()
    init_db(config)
    yield
    # 종료: 정리 작업 (현재 없음)


app = FastAPI(
    title="maipharm-domae-mcp",
    description="의약품 도매 통합 검색/주문 로컬 서버",
    version="1.0.0",
    lifespan=lifespan,
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
    """웹서버 실행 (127.0.0.1 바인딩)"""
    uvicorn.run(
        app,
        host="127.0.0.1",
        port=port,
        log_level="info",
    )
