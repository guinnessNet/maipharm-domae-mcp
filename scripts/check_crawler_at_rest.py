"""워커 환경의 크롤러 키로 모든 크롤러 행이 풀리는지 확인한다(A3). 개수·이름·참거짓만 출력한다.

사용: 워커 서비스의 env 로딩 방식 그대로, 워커 체크아웃 밖의 임시 위치에 복사한 이 파일을 워커 venv 의 Python 으로 실행한다.
  python check_crawler_at_rest.py [--final]
출력 currentUsed·canRestoreOldKeyOnly 는 런북 G3 롤백 판단에 쓴다(새 키로 열리는 행이 하나라도 있으면 env 를 되돌리지 않는다).
종료 코드: 0 통과, 1 행 실패·hash 불일치·0행, 2 키 설정 오류, 3 DB·기타 오류,
          4 --final 조건 미충족(이전 키 설정·사용, 크롤러 키 = 크리덴셜 키)
워커 코드(domae_mcp)를 import 하지 않는다. 배포 전 환경에서도 같은 판정을 내기 위해서다.
"""
import base64
import hashlib
import hmac
import json
import os
import sys

import psycopg2
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def _key(name: str):
    b64 = os.environ.get(name)
    if not b64:
        return None
    key = base64.b64decode(b64)
    if len(key) != 32:
        raise ValueError(name)
    return key


def main(argv) -> int:
    final = "--final" in argv
    try:
        current = _key("DOMAE_CRAWLER_KEY")
        previous = _key("DOMAE_CRAWLER_KEY_PREVIOUS")
        credential = _key("DOMAE_CREDENTIAL_KEY")
    except ValueError as e:
        print(json.dumps({"key": "wrong_length", "name": str(e)}))
        return 2
    if current is None:
        print(json.dumps({"key": "missing"}))
        return 2
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    try:
        with conn.cursor() as cur:
            cur.execute('SELECT name, code, "codeHash" FROM domae_crawlers')
            rows = cur.fetchall()
    finally:
        conn.close()
    failed, previous_used, current_used, hash_mismatch = [], [], [], []
    for name, code, code_hash in rows:
        try:
            if not code.startswith("v1:"):
                raise ValueError("legacy")
            raw = base64.b64decode(code[3:])
            try:
                plain = AESGCM(current).decrypt(raw[:12], raw[12:], None)
                used = current_used
            except Exception:
                if previous is None:
                    raise
                plain = AESGCM(previous).decrypt(raw[:12], raw[12:], None)
                used = previous_used
            text = plain.decode("utf-8")
            used.append(name)
            if hashlib.sha256(text.encode("utf-8")).hexdigest() != code_hash:
                hash_mismatch.append(name)
        except Exception:
            failed.append(name)
    same_as_credential = credential is not None and hmac.compare_digest(credential, current)
    # canRestoreOldKeyOnly: 키 교체 중 env 를 옛 상태로 되돌려도 되는지(모든 행이 옛(이전) 키로만 열림). 런북 G3 롤백 판단
    can_restore = (previous is not None and bool(rows) and not failed and not current_used
                   and len(previous_used) == len(rows))
    print(json.dumps({"total": len(rows), "failed": failed, "previousUsed": previous_used, "currentUsed": current_used,
                      "hashMismatch": hash_mismatch, "previousConfigured": previous is not None,
                      "sameAsCredential": same_as_credential, "canRestoreOldKeyOnly": can_restore}))
    if not rows or failed or hash_mismatch:
        return 1
    if final and (previous is not None or previous_used or same_as_credential):
        return 4
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except Exception:  # 예외 원문·연결 문자열을 출력하지 않는다
        print(json.dumps({"error": "check_failed"}))
        sys.exit(3)
