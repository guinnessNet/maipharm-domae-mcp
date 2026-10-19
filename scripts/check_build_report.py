"""exe 빌드 보고서로 포함 모듈을 검사한다(P18·P19, 1차 검수 m1). 압축된 exe 바이트는 보지 않는다.

사용: python scripts/check_build_report.py <report> --kind nuitka|pyinstaller
- nuitka: --report 로 만든 XML(<module name="..."> 원소)
- pyinstaller: build/<name>/ 의 PYZ-00.toc(또는 그 폴더)
금지 모듈이 없고 필수 모듈이 있으면 rc 0, 아니면 rc 1(모듈 이름만 출력). 입력 오류 rc 2.
"""
import argparse
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

FORBIDDEN_EXACT = {"domae_mcp.core.crawlers.loader", "domae_mcp.core.crawlers._client_key"}
FORBIDDEN_PREFIX = ("domae_mcp.cloud", "pytest", "_pytest", "fakeredis", "tests")
ALLOWED_CRAWLER_PKG = {"domae_mcp.core.crawlers", "domae_mcp.core.crawlers.base", "domae_mcp.core.crawlers.registry",
                       "domae_mcp.core.crawlers.cart_snapshot"}
REQUIRED = ("domae_mcp.v2.crawler_deps", "domae_mcp.v2.gate", "domae_mcp._build_info", "requests", "urllib3", "bs4",
            "lxml")
REQUIRED_STDLIB = ("re", "json", "logging", "time", "math", "base64", "os", "inspect", "typing", "datetime",
                   "urllib")
_TOC = re.compile(r"\(\s*'([^']+)'\s*,\s*(?:'[^']*'|None)\s*,\s*'PYMODULE'\s*\)")


def nuitka_modules(path: Path) -> set[str]:
    root = ET.parse(path).getroot()
    return {el.get("name") for el in root.iter("module") if el.get("name")}


def pyinstaller_modules(path: Path) -> set[str]:
    files = [path] if path.is_file() else sorted(path.glob("PYZ-*.toc"))
    out = set()
    for f in files:
        out |= set(_TOC.findall(f.read_text(encoding="utf-8", errors="replace")))
    return out


def _has(mods: set[str], name: str) -> bool:
    return name in mods or any(m.startswith(name + ".") for m in mods)


def problems(mods: set[str], stdlib_listed: bool) -> list[str]:
    bad = []
    for m in sorted(mods):
        if m in FORBIDDEN_EXACT or "familypharm" in m or any(m == p or m.startswith(p + ".") for p in FORBIDDEN_PREFIX):
            bad.append(f"forbidden:{m}")
        elif m.startswith("domae_mcp.core.crawlers.") and m not in ALLOWED_CRAWLER_PKG:
            bad.append(f"forbidden:{m}")
    for r in REQUIRED + (REQUIRED_STDLIB if stdlib_listed else ()):
        if r in sys.builtin_module_names:  # time·math 등 인터프리터 내장 모듈은 보고서에 나오지 않는다
            continue
        if not _has(mods, r):
            bad.append(f"missing:{r}")
    return bad


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("report", type=Path)
    ap.add_argument("--kind", choices=["nuitka", "pyinstaller"], required=True)
    a = ap.parse_args(argv)
    if not a.report.exists():
        print("report missing")
        return 2
    try:
        mods = nuitka_modules(a.report) if a.kind == "nuitka" else pyinstaller_modules(a.report)
    except (ET.ParseError, OSError):
        print("report unreadable")
        return 2
    if not mods:
        print("no modules in report")
        return 2
    # Nuitka 보고서는 표준 라이브러리 모듈도 담으므로 항상 검사한다. PyInstaller TOC 는 담을 때만 검사한다
    stdlib_listed = a.kind == "nuitka" or _has(mods, "json") or _has(mods, "logging")
    bad = problems(mods, stdlib_listed)
    for b in bad:
        print(b)
    print(f"modules={len(mods)} problems={len(bad)} stdlib_listed={stdlib_listed}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
