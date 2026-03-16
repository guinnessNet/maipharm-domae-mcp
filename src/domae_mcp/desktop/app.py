"""Windows 데스크톱 앱: 트레이 아이콘 + 서버 + 자동 업데이트"""

import logging
import sys
import threading
import webbrowser

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


def main():
    """데스크톱 앱 진입점."""
    from domae_mcp.desktop.tray import TrayApp
    app = TrayApp()
    app.run()


if __name__ == "__main__":
    main()
