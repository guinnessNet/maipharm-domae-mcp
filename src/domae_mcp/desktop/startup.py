"""Windows 시작프로그램 관리 (exe 및 python 모두 지원)"""

import os
import sys
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

STARTUP_NAME = "MaipharmDomae"


def _get_startup_dir() -> Path:
    """Windows 시작프로그램 폴더."""
    appdata = os.environ.get("APPDATA", "")
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def _get_shortcut_path() -> Path:
    """바로가기 파일 경로."""
    return _get_startup_dir() / f"{STARTUP_NAME}.lnk"


def _get_exe_path() -> str:
    """현재 실행 파일 경로 (exe 또는 python)."""
    if getattr(sys, 'frozen', False):
        # PyInstaller exe
        return sys.executable
    else:
        # python -m domae_mcp.desktop.app
        return f'"{sys.executable}" -m domae_mcp.desktop.app'


def is_startup_registered() -> bool:
    """시작프로그램 등록 여부 확인."""
    shortcut = _get_shortcut_path()
    if shortcut.exists():
        return True
    # VBS 방식도 체크 (이전 버전 호환)
    vbs = _get_startup_dir() / "maipharm-domae-mcp.vbs"
    return vbs.exists()


def toggle_startup(enable: bool):
    """시작프로그램 등록/해제."""
    shortcut_path = _get_shortcut_path()

    if enable:
        try:
            if getattr(sys, 'frozen', False):
                # PyInstaller exe
                target_path = sys.executable
                arguments = ""
            else:
                # python -m domae_mcp.desktop.app
                target_path = sys.executable
                arguments = "-m domae_mcp.desktop.app"

            ps_cmd = (
                '$ws = New-Object -ComObject WScript.Shell; '
                f'$s = $ws.CreateShortcut("{shortcut_path}"); '
                f'$s.TargetPath = "{target_path}"; '
                f'$s.Arguments = "{arguments}"; '
                '$s.WindowStyle = 7; '
                '$s.Description = "Maipharm Domae"; '
                '$s.Save()'
            )
            os.system(f'powershell -Command "{ps_cmd}"')
            logger.info("시작프로그램 등록: %s", shortcut_path)
        except Exception as e:
            logger.error("시작프로그램 등록 실패: %s", e)
    else:
        if shortcut_path.exists():
            shortcut_path.unlink()
            logger.info("시작프로그램 해제: %s", shortcut_path)
        # 이전 VBS 방식도 정리
        vbs = _get_startup_dir() / "maipharm-domae-mcp.vbs"
        if vbs.exists():
            vbs.unlink()
