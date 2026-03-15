"""SQLite 데이터베이스: WAL 모드, ~/.maipharm-domae-mcp/data/domae.db"""

from collections.abc import Generator
from pathlib import Path

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from domae_mcp.core.models import Base, MonitorSchedule
from domae_mcp.local.config import ConfigManager


def _get_db_path(config: ConfigManager | None = None) -> Path:
    """DB 파일 경로 반환"""
    if config:
        base_dir = config.base_dir
    else:
        base_dir = Path.home() / ".maipharm-domae-mcp"
    data_dir = base_dir / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir / "domae.db"


def _create_engine(db_path: Path) -> Engine:
    """SQLite 엔진 생성 (WAL 모드, check_same_thread=False)"""
    url = f"sqlite:///{db_path}"
    engine = create_engine(
        url,
        connect_args={"check_same_thread": False, "timeout": 5},
    )

    # 모든 연결에서 WAL 모드 활성화
    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.close()

    return engine


# 모듈 레벨 엔진/세션 (lazy init)
_engine: Engine | None = None
_SessionLocal: sessionmaker[Session] | None = None


def _get_engine(config: ConfigManager | None = None) -> Engine:
    """엔진 싱글톤 반환"""
    global _engine
    if _engine is None:
        db_path = _get_db_path(config)
        _engine = _create_engine(db_path)
    return _engine


def _get_session_factory(config: ConfigManager | None = None) -> sessionmaker[Session]:
    """세션 팩토리 싱글톤 반환"""
    global _SessionLocal
    if _SessionLocal is None:
        engine = _get_engine(config)
        _SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    return _SessionLocal


def _seed_default_schedules(session: Session) -> None:
    """기본 모니터링 스케줄 시딩 (테이블이 비어있을 때만)"""
    existing = session.query(MonitorSchedule).first()
    if existing:
        return

    defaults = [
        MonitorSchedule(start_hour=0, end_hour=8, interval_minutes=120),
        MonitorSchedule(start_hour=8, end_hour=22, interval_minutes=60),
        MonitorSchedule(start_hour=22, end_hour=24, interval_minutes=120),
    ]
    session.add_all(defaults)
    session.commit()


def init_db(config: ConfigManager | None = None) -> None:
    """DB 초기화: 테이블 생성 + 기본 스케줄 시딩"""
    engine = _get_engine(config)
    Base.metadata.create_all(bind=engine)

    session_factory = _get_session_factory(config)
    session = session_factory()
    try:
        _seed_default_schedules(session)
    finally:
        session.close()


def get_db() -> Generator[Session, None, None]:
    """FastAPI Depends용 세션 제너레이터"""
    session_factory = _get_session_factory()
    session = session_factory()
    try:
        yield session
    finally:
        session.close()
