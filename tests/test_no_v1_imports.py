"""v1 로더·고정 전송 키·v1 서버 경로가 남지 않았는지 정적으로 확인한다 (계획 Task 10, B 정리)."""
import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BANNED_MODULES = {"domae_mcp.core.crawlers.loader", "domae_mcp.core.crawlers._client_key"}
V1_URL = re.compile(r"api\.pharmsq\.com/api/domae|/api/domae/(verify|heartbeat|search-permit)|crawlers/version"
                    r"|encrypted=1")


def _module_name(path: Path) -> tuple[str, bool]:
    """(모듈 이름, 패키지 __init__ 여부). src 밖(scripts)은 상대 import 가 없으므로 빈 이름."""
    try:
        parts = list(path.relative_to(ROOT / "src").with_suffix("").parts)
    except ValueError:
        return "", False
    is_init = bool(parts) and parts[-1] == "__init__"
    return ".".join(parts[:-1] if is_init else parts), is_init


def _absolute(modname: str, is_init: bool, node: ast.ImportFrom) -> str:
    if not node.level:
        return node.module or ""
    pkg = modname.split(".") if is_init else modname.split(".")[:-1]
    if node.level > 1:
        pkg = pkg[:len(pkg) - (node.level - 1)]
    return ".".join(pkg + ([node.module] if node.module else []))


def scan(source: str, modname: str = "", is_init: bool = False) -> list[str]:
    out = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            out += [a.name for a in node.names if a.name in BANNED_MODULES]
        elif isinstance(node, ast.ImportFrom):
            mod = _absolute(modname, is_init, node)
            if mod in BANNED_MODULES or any(f"{mod}.{a.name}" in BANNED_MODULES for a in node.names):
                out.append(mod)
            if any("CrawlerLoader" in (a.name, a.asname) for a in node.names):
                out.append("CrawlerLoader")
        elif isinstance(node, ast.Name) and node.id == "CrawlerLoader":
            out.append("CrawlerLoader")
        elif isinstance(node, ast.Attribute) and node.attr == "CrawlerLoader":
            out.append("CrawlerLoader")
    return out


def _py_files():
    for base in ("src", "scripts"):
        yield from sorted((ROOT / base).rglob("*.py"))


def test_no_v1_imports_in_src_and_scripts():
    bad = {}
    for p in _py_files():
        modname, is_init = _module_name(p)
        v = scan(p.read_text(encoding="utf-8"), modname, is_init)
        if v:
            bad[str(p.relative_to(ROOT))] = v
    assert bad == {}


def test_no_v1_files_left():
    for name in ("loader.py", "_client_key.py", "familypharm.py"):
        assert not (ROOT / "src" / "domae_mcp" / "core" / "crawlers" / name).exists()


def test_spec_hiddenimports_have_no_v1():
    text = (ROOT / "domae.spec").read_text(encoding="utf-8")
    m = re.search(r"hiddenimports=\[(.*?)\]", text, re.S)
    assert m
    names = set(re.findall(r"'([^']+)'", m.group(1)))
    assert not (names & (BANNED_MODULES | {"domae_mcp.local.api_key"}))


def test_no_v1_server_urls_in_src():
    hits = []
    for p in sorted((ROOT / "src").rglob("*.py")):
        for no, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            if V1_URL.search(line):
                hits.append(f"{p.relative_to(ROOT)}:{no}")
    assert hits == []


@pytest.mark.parametrize("src", [
    "from domae_mcp.core.crawlers.loader import CrawlerLoader\n",
    "import domae_mcp.core.crawlers.loader\n",
    "from domae_mcp.core.crawlers import CrawlerLoader\n",
    "from domae_mcp.core.crawlers import loader\n",
    "from domae_mcp.core.crawlers._client_key import X\n",
    "x = CrawlerLoader()\n",
    "from .loader import Thing as L\n",
    "from . import _client_key\n",
])
def test_detector_catches_synthetic(src):
    assert scan(src, "domae_mcp.core.crawlers.registry")


def test_detector_relative_from_package_init():
    assert scan("from .loader import x\n", "domae_mcp.core.crawlers", is_init=True)
    assert scan("from ..crawlers import loader\n", "domae_mcp.core.services.search_service")


def test_detector_ignores_strings():
    assert scan("s = 'domae_mcp.core.crawlers.loader'  # 문자열은 대상이 아니다\n") == []
