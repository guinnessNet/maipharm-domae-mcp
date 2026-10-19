"""exe 숨은 인자 --v2-selftest-out <file> (B-4 Task 11). 빌드 종류·신뢰 목록 id·런타임만 JSON 파일로 쓴다.

콘솔 없는 GUI exe 라 stdout 대신 파일로 쓴다. 공개키 id 만 담고 비밀은 담지 않는다. 성공 0, 실패 1.
"""
import json
import sys
from pathlib import Path

FLAG = "--v2-selftest-out"


def run(out: str) -> int:
    report = {"kind": None, "version": None, "runtime_id": None, "trust_ids": [], "deps_ok": False}
    code = 1
    try:
        from domae_mcp import __version__
        from domae_mcp.v2 import crawler_deps
        from domae_mcp.v2.runtime import build_kind, local_runtime_id
        from domae_mcp.v2.trust import active_trust
        report.update(kind=build_kind(), version=__version__, runtime_id=local_runtime_id(),
                      trust_ids=sorted(active_trust()), deps_ok=crawler_deps.check())
        code = 0 if report["deps_ok"] else 1
    except Exception as e:  # 사유 이름만 남긴다
        report["error"] = type(e).__name__
    Path(out).write_text(json.dumps(report, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    return code


def maybe_run(argv: list[str]) -> None:
    """argv 에 FLAG 가 있으면 자체 시험을 하고 프로세스를 끝낸다."""
    if FLAG in argv:
        i = argv.index(FLAG)
        if i + 1 >= len(argv):
            sys.exit(1)
        sys.exit(run(argv[i + 1]))
