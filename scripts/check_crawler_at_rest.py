"""워커 환경의 크롤러 키로 모든 크롤러 행이 풀리는지 확인한다. 개수와 이름만 출력한다.

사용: 준비 단계에서 확정한 워커 서비스의 env 로딩 방식 그대로 /tmp/domae-at-rest-check/check_crawler_at_rest.py 를 워커 venv 의 Python 으로 실행한다. 완성된 실행 명령(경로만, 값 없이)을 배포 기록에 적은 뒤 실행한다.
"""
import base64
import json
import os
import sys

import psycopg2
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def main() -> int:
    b64 = os.environ.get("DOMAE_CRAWLER_KEY")
    if not b64:
        print(json.dumps({"key": "missing"}))
        return 2
    key = base64.b64decode(b64)
    if len(key) != 32:
        print(json.dumps({"key": "wrong_length"}))
        return 2
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT name, code FROM domae_crawlers")
            rows = cur.fetchall()
    finally:
        conn.close()
    failed = []
    for name, code in rows:
        try:
            if not code.startswith("v1:"):
                raise ValueError("legacy")
            raw = base64.b64decode(code[3:])
            AESGCM(key).decrypt(raw[:12], raw[12:], None).decode("utf-8")
        except Exception:
            failed.append(name)
    print(json.dumps({"total": len(rows), "failed": failed}))
    return 1 if (not rows or failed) else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # 예외 원문·연결 문자열을 출력하지 않는다
        print(json.dumps({"error": "check_failed"}))
        sys.exit(3)
