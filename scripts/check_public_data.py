"""공개 레포에 실측값이 들어가지 않았는지 검사한다. 일치한 값은 출력하지 않는다.

금지 목록은 도매 아이디·비밀번호 같은 원값을 한 줄에 하나씩 적은 비공개 파일이다(도매 구분·순서 불필요).
줄 안의 공백은 비교할 때 무시한다.
경로는 PUBLIC_DATA_DENYLIST 로 받고, 없으면 레포 루트의 .public-data-denylist(gitignore 대상)를 쓴다.
CI 는 Actions 비밀값 PUBLIC_DATA_DENYLIST 를 임시 파일로 써서 넘긴다.
목록이 없거나 비었거나 4자 미만 값이 있으면 검사를 건너뛰지 않고 종료 코드 2 로 실패한다.
"""
import os
import re
import sys
from pathlib import Path

DEFAULT_DENYLIST = Path(__file__).resolve().parents[1] / ".public-data-denylist"
MIN_LEN = 4
WS = re.compile(r"\s+")
PATTERNS = {
    "chat_id": re.compile(r"(?i)chat_?id\D{0,6}(-?\d{9,})"),
}
SYNTHETIC_CHAT_ID = re.compile(r"-?10{7,}\d?")


def load_denylist():
    env = os.environ.get("PUBLIC_DATA_DENYLIST")
    path = Path(env) if env else DEFAULT_DENYLIST
    try:
        values = [l.strip() for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    except (OSError, UnicodeDecodeError):
        return None
    # 공백을 무시하고 비교한다(예: 중간에 띄어쓰기가 있는 아이디를 붙여 쓴 경우도 잡는다)
    values = [WS.sub("", v) for v in values]
    if not values or any(len(v) < MIN_LEN for v in values):
        return None
    return values


def scan(path: Path, deny: list) -> list:
    hits = []
    for no, line in enumerate(path.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
        for name, pat in PATTERNS.items():
            for m in pat.finditer(line):
                if name == "chat_id" and SYNTHETIC_CHAT_ID.fullmatch(m.group(1)):
                    continue
                hits.append((no, name))
        compact = WS.sub("", line)
        if any(v in compact for v in deny):
            hits.append((no, "denylisted"))
    return hits


def main(argv: list) -> int:
    deny = load_denylist()
    if deny is None:
        print("config_error: denylist missing, unreadable, empty or has values shorter than 4 chars")
        return 2
    bad = 0
    for arg in argv:
        for p in ([Path(arg)] if Path(arg).is_file() else Path(arg).rglob("*.py")):
            for no, name in scan(p, deny):
                print(f"{p}:{no}: {name}")
                bad += 1
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:] or ["tests", "src"]))
