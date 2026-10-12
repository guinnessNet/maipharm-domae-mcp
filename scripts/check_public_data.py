"""공개 레포에 실측값이 들어가지 않았는지 검사한다. 일치한 값은 출력하지 않는다.

금지 목록(거래처 코드·약국명의 SHA-256)은 비공개로 관리하며 PUBLIC_DATA_DENYLIST 로 경로를 받는다.
목록이 없거나 잘못되면 검사를 건너뛰지 않고 종료 코드 2 로 실패한다.
"""
import hashlib
import os
import re
import sys
from pathlib import Path

SYNTHETIC_CODE = re.compile(r"^699\d{6}$")
DIGEST = re.compile(r"^[0-9a-f]{64}$")
# 거래처 코드·약국명 후보: 따옴표 안의 영숫자 코드(4~12자)와 '약국'·'팜'으로 끝나는 이름(공백 허용)
CANDIDATE = re.compile(r"['\"]([A-Za-z0-9]{4,12}|[가-힣A-Za-z0-9 ]{1,20}(?:약국|팜))['\"]")
PATTERNS = {
    # 16진수 해시(커밋 SHA 등) 안의 숫자열은 보험코드가 아니므로 16진 문자가 붙은 경우는 제외한다
    "insurance_code": re.compile(r"(?<![0-9a-fA-F])\d{9}(?![0-9a-fA-F])"),
    "chat_id": re.compile(r"(?i)chat_?id\D{0,6}(-?\d{9,})"),
}


def load_denylist():
    path = os.environ.get("PUBLIC_DATA_DENYLIST")
    if not path:
        return None
    try:
        lines = [l.strip() for l in Path(path).read_text(encoding="utf-8").splitlines() if l.strip()]
    except OSError:
        return None
    if not lines or not all(DIGEST.match(l) for l in lines):
        return None
    return set(lines)


def scan(path: Path, deny: set) -> list:
    hits = []
    for no, line in enumerate(path.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
        for name, pat in PATTERNS.items():
            for m in pat.finditer(line):
                value = m.group(m.lastindex or 0)
                if name == "insurance_code" and (SYNTHETIC_CODE.match(value) or len(set(value)) == 1):
                    continue
                if name == "chat_id" and re.fullmatch(r"-?10{7,}\d?", value):
                    continue
                hits.append((no, name))
        for m in CANDIDATE.finditer(line):
            if hashlib.sha256("".join(m.group(1).split()).encode("utf-8")).hexdigest() in deny:
                hits.append((no, "denylisted"))
    return hits


def main(argv: list) -> int:
    deny = load_denylist()
    if deny is None:
        print("config_error: PUBLIC_DATA_DENYLIST missing, unreadable, empty or malformed")
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
