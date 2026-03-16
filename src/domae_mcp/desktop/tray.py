"""시스템 트레이 아이콘"""

import logging
import threading
import webbrowser
import sys

from PIL import Image, ImageDraw
import pystray

logger = logging.getLogger(__name__)

# 서버 상태
_server_thread = None
_server_running = False


def _create_icon_image(color="green"):
    """트레이 아이콘 이미지 생성 (16x16 원형)."""
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    colors = {"green": (34, 197, 94), "gray": (156, 163, 175), "orange": (249, 115, 22)}
    c = colors.get(color, colors["gray"])
    draw.ellipse([8, 8, 56, 56], fill=c)
    # "D" 글자
    draw.text((22, 14), "D", fill="white")
    return img


def _start_server():
    """FastAPI 서버를 백그라운드 스레드로 시작."""
    global _server_thread, _server_running
    if _server_running:
        return

    def run():
        global _server_running
        _server_running = True
        try:
            from domae_mcp.local.server import run_web_server
            run_web_server()
        except Exception as e:
            logger.error("서버 에러: %s", e)
        finally:
            _server_running = False

    _server_thread = threading.Thread(target=run, daemon=True)
    _server_thread.start()


def _open_browser(icon=None, item=None):
    """브라우저에서 localhost:5900 열기."""
    from domae_mcp.local.config import ConfigManager
    config = ConfigManager()
    port = config.get_port()
    webbrowser.open(f"http://localhost:{port}")


def _check_update(icon=None, item=None):
    """GitHub Release에서 업데이트 확인."""
    from domae_mcp.desktop.updater import check_for_update
    result = check_for_update()
    if result:
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk()
        root.withdraw()
        if messagebox.askyesno("업데이트", f"새 버전 {result['tag']}이 있습니다.\n다운로드 페이지를 열까요?"):
            webbrowser.open(result["url"])
        root.destroy()
    else:
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk()
        root.withdraw()
        messagebox.showinfo("업데이트", "최신 버전입니다.")
        root.destroy()


def _toggle_startup(icon=None, item=None):
    """Windows 시작프로그램 등록/해제 토글."""
    from domae_mcp.desktop.startup import is_startup_registered, toggle_startup
    current = is_startup_registered()
    toggle_startup(not current)
    # 메뉴 텍스트 업데이트를 위해 아이콘 업데이트
    icon.update_menu()


def _is_startup_checked(item):
    """시작프로그램 등록 여부 확인 (메뉴 체크 표시용)."""
    from domae_mcp.desktop.startup import is_startup_registered
    return is_startup_registered()


def _quit(icon, item):
    """앱 종료."""
    icon.stop()


class TrayApp:
    """시스템 트레이 앱."""

    def run(self):
        """트레이 아이콘 + 서버 시작."""
        # 서버 시작
        _start_server()

        # 2초 후 브라우저 열기 (첫 실행 시)
        timer = threading.Timer(2.0, _open_browser)
        timer.daemon = True
        timer.start()

        # 트레이 아이콘 메뉴
        menu = pystray.Menu(
            pystray.MenuItem("브라우저 열기", _open_browser, default=True),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("업데이트 확인", _check_update),
            pystray.MenuItem("시작프로그램 등록", _toggle_startup, checked=_is_startup_checked),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("종료", _quit),
        )

        icon = pystray.Icon(
            "domae-mcp",
            icon=_create_icon_image("green"),
            title="도매 통합검색",
            menu=menu,
        )

        logger.info("트레이 아이콘 시작")
        icon.run()
