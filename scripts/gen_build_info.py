"""빌드 종류 파일(src/domae_mcp/_build_info.py)을 쓴다/지운다 (P3). 빌드 스크립트 전용, 커밋하지 않는다(.gitignore).

사용: python scripts/gen_build_info.py --kind test|release [--out <path>]
      python scripts/gen_build_info.py --remove [--out <path>]
"""
import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = ROOT / "src" / "domae_mcp" / "_build_info.py"


def git_sha(root: Path) -> str:
    try:
        out = subprocess.run(["git", "-C", str(root), "rev-parse", "--short=12", "HEAD"], capture_output=True,
                             text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"
    return out if out.isalnum() else "unknown"


def render(kind: str, version: str, sha: str) -> str:
    if kind not in ("test", "release"):
        raise ValueError("kind")
    for v in (version, sha):
        if not all(ch.isalnum() or ch in ".-" for ch in v):
            raise ValueError("value")
    return f'"""빌드 스크립트가 만든 파일. 커밋하지 않는다."""\nKIND = "{kind}"\nVERSION = "{version}"\nGIT_SHA = "{sha}"\n'


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--kind", choices=["test", "release"])
    ap.add_argument("--remove", action="store_true")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    a = ap.parse_args(argv)
    if a.remove:
        a.out.unlink(missing_ok=True)
        print("removed")
        return 0
    if not a.kind:
        ap.error("--kind 또는 --remove")
    sys.path.insert(0, str(ROOT / "src"))
    from domae_mcp import __version__
    a.out.write_text(render(a.kind, __version__, git_sha(ROOT)), encoding="utf-8")
    print(f"kind={a.kind} version={__version__}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
