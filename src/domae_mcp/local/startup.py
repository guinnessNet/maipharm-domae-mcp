"""Windows 자동시작: VBS 스크립트로 시작프로그램 등록/해제"""

import platform
from pathlib import Path

VBS_FILENAME = "maipharm-domae-mcp.vbs"

VBS_CONTENT = """\
' maipharm-domae-mcp 자동시작
Set WshShell = CreateObject("WScript.Shell")
WshShell.Run "pythonw -m domae_mcp", 0, False
"""


def _get_startup_dir() -> Path:
    """Windows 시작프로그램 폴더 경로"""
    import os
    appdata = os.environ.get("APPDATA", "")
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def install_startup() -> None:
    """Windows 시작프로그램에 VBS 스크립트 등록"""
    if platform.system() != "Windows":
        print("이 기능은 Windows에서만 지원됩니다.")
        return

    startup_dir = _get_startup_dir()
    if not startup_dir.exists():
        print(f"시작프로그램 폴더를 찾을 수 없습니다: {startup_dir}")
        return

    vbs_path = startup_dir / VBS_FILENAME
    vbs_path.write_text(VBS_CONTENT, encoding="utf-8")
    print(f"자동시작이 등록되었습니다: {vbs_path}")


def uninstall_startup() -> None:
    """Windows 시작프로그램에서 VBS 스크립트 제거"""
    if platform.system() != "Windows":
        print("이 기능은 Windows에서만 지원됩니다.")
        return

    startup_dir = _get_startup_dir()
    vbs_path = startup_dir / VBS_FILENAME

    if vbs_path.exists():
        vbs_path.unlink()
        print(f"자동시작이 해제되었습니다: {vbs_path}")
    else:
        print("등록된 자동시작이 없습니다.")
