"""Windows 데스크톱 앱: 트레이 아이콘 + 서버 + 자동 업데이트"""

import logging
import sys
import threading
import webbrowser

logger = logging.getLogger(__name__)


def _setup_logging() -> None:
    """콘솔 없는 GUI exe 는 sys.stdout/stderr 가 None 이다(uvicorn 로그 설정이 isatty 에서 실패한다).
    그때는 앱 홈 logs/app.log 로 보낸다. 비밀은 로그에 남기지 않는다(v2 모듈 규칙)."""
    fmt = "%(asctime)s [%(name)s] %(levelname)s: %(message)s"
    if sys.stdout is None or sys.stderr is None:
        from domae_mcp.v2.store import app_home
        log_dir = app_home() / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        stream = open(log_dir / "app.log", "a", encoding="utf-8", buffering=1)
        if sys.stdout is None:
            sys.stdout = stream
        if sys.stderr is None:
            sys.stderr = stream
        logging.basicConfig(level=logging.INFO, format=fmt, stream=stream)
    else:
        logging.basicConfig(level=logging.INFO, format=fmt)


def main():
    """데스크톱 앱 진입점."""
    from domae_mcp.v2.selftest import maybe_run
    maybe_run(sys.argv[1:])  # --v2-selftest-out <file> 이면 자체 시험 뒤 종료
    _setup_logging()
    from domae_mcp.desktop.tray import TrayApp
    app = TrayApp()
    app.run()


if __name__ == "__main__":
    main()
