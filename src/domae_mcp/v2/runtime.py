"""도매 v2 런타임 식별·빌드 종류 (스펙 B3 runtime_id, P3). release 빌드는 개발 재정의를 읽지 않는다."""
import importlib
import importlib.util
import json
import logging
import os
import platform
import sys
import sysconfig

from .encoding import encode_runtime_id

logger = logging.getLogger(__name__)

LOADER_ABI = 1
_KINDS = ("test", "release")
_warned: set[str] = set()


def build_kind() -> str:
    """source = 레포에서 실행(_build_info 없음). test·release = 빌드 스크립트가 만든 _build_info.KIND.
    알 수 없는 값은 release 로 본다(개발 재정의를 막는 쪽)."""
    try:
        info = importlib.import_module("domae_mcp._build_info")
    except ModuleNotFoundError as e:
        if e.name == "domae_mcp._build_info":
            return "source"
        return "release"
    except Exception:
        return "release"
    kind = getattr(info, "KIND", None)
    return kind if kind in _KINDS else "release"


def dev_override(name: str) -> str | None:
    value = os.environ.get(name)
    if build_kind() == "release":
        if value and name not in _warned:
            _warned.add(name)
            logger.warning("release 빌드는 개발 재정의를 무시한다: %s", name)
        return None
    return value


def local_runtime_id() -> dict:
    path = dev_override("DOMAE_V2_RUNTIME_ID_FILE")
    if path:
        with open(path, encoding="utf-8") as f:
            rid = json.load(f)
        encode_runtime_id(rid)  # 형식이 틀리면 EncodingError
        return rid
    plat = "win_amd64" if sys.platform == "win32" and platform.machine() == "AMD64" else sysconfig.get_platform()
    return {
        "implementation": sys.implementation.name,
        "python_version": platform.python_version(),
        "magic": importlib.util.MAGIC_NUMBER.hex(),
        "artifact_kind": "marshal-code",
        "loader_abi": LOADER_ABI,
        "platform": plat,
    }
