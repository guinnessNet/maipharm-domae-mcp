"""Windows 데스크톱 앱: 트레이 아이콘 + 서버 + 자동 업데이트"""

import logging
import sys
import threading
import webbrowser

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


def main():
    """데스크톱 앱 진입점."""
    from domae_mcp.v2.selftest import maybe_run
    maybe_run(sys.argv[1:])  # --v2-selftest-out <file> 이면 자체 시험 뒤 종료
    from domae_mcp.desktop.tray import TrayApp
    app = TrayApp()
    app.run()


if __name__ == "__main__":
    main()
