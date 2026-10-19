"""seeds 크롤러의 import 가 exe 포함 목록(domae_mcp.v2.crawler_deps) 안에 있는지 확인한다(P18).

사용: python scripts/check_crawler_imports.py <seeds_dir>
- domae_mcp.* 는 내부 ABI 허용 목록과 전체 경로가 같아야 한다.
- 그 밖은 최상위 이름이 외부 허용 목록 안에 있어야 한다. 상대 import 는 거부한다.
목록 밖 모듈 이름만 출력한다(크롤러 원문·경로 세부는 출력하지 않는다). rc 0 = 통과, 1 = 목록 밖 있음, 2 = 입력 오류.
"""
import ast
import sys
from pathlib import Path

EXTERNAL_TOP = ("re", "json", "logging", "time", "math", "base64", "os", "inspect", "typing", "datetime", "urllib",
                "requests", "urllib3", "bs4", "lxml")
INTERNAL_ABI = ("domae_mcp.core.crawlers.base", "domae_mcp.core.crawlers.cart_snapshot")


def imported_modules(source: str) -> set[str]:
    out = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            out.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            out.add("." * node.level + (node.module or ""))
    return out


def violations(modules: set[str]) -> set[str]:
    bad = set()
    for m in modules:
        if not m or m.startswith("."):
            bad.add(m or "<empty>")
        elif m == "domae_mcp" or m.startswith("domae_mcp."):
            if m not in INTERNAL_ABI:
                bad.add(m)
        elif m.split(".")[0] not in EXTERNAL_TOP:
            bad.add(m)
    return bad


def main(argv: list[str]) -> int:
    if len(argv) != 1 or not Path(argv[0]).is_dir():
        print("usage: check_crawler_imports.py <seeds_dir>")
        return 2
    files = sorted(Path(argv[0]).rglob("*.py"))
    if not files:
        print("no python files")
        return 2
    modules = set()
    for f in files:
        modules |= imported_modules(f.read_text(encoding="utf-8"))
    bad = violations(modules)
    for m in sorted(bad):
        print(m)
    print(f"files={len(files)} modules={len(modules)} outside={len(bad)}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
