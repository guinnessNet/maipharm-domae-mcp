"""
도매 크롤러 통합 테스트 (domae-v2 시드 계정 사용)
Usage: python -m tests.test_crawlers [도매상명]
"""
import sys
import time
import traceback

sys.path.insert(0, "src")

from domae_mcp.core.crawlers.registry import CrawlerRegistry

# domae-v2 _seed_credentials() 기반
SEED_CREDENTIALS = {
    "지오영": {"login_id": "ptopen pharm", "login_pw": "open8864"},
    "복산": {"login_id": "ptopenpharm", "login_pw": "open8864"},
    "인천": {"login_id": "ptopenpharm", "login_pw": "open8864!!"},
    "티제이팜": {"login_id": "0107615", "login_pw": "2900"},
    "HMP": {"login_id": "ptopenpharm", "login_pw": "open2900!!"},
    "백제": {"login_id": "2024782", "login_pw": "a123456"},
    "피코": {"login_id": "ptopenpharm", "login_pw": "open8864!!"},
    "새로팜": {"login_id": "ptopen2900", "login_pw": "open2900"},
    "신덕팜": {"login_id": "starlightph1", "login_pw": "qwertyu71!"},
    "대전동원약품": {"login_id": "starlightph1", "login_pw": "qwertyu71!"},
}

TEST_KEYWORD = "아목시실린"


def test_crawler(name: str):
    """단일 크롤러 로그인 + 검색 테스트"""
    cred = SEED_CREDENTIALS.get(name)
    if not cred:
        print(f"  ❌ 계정 정보 없음")
        return False

    crawler = CrawlerRegistry.get(name)

    # 1) 로그인
    print(f"  로그인 시도... ", end="", flush=True)
    start = time.time()
    try:
        ok = crawler.login(cred["login_id"], cred["login_pw"])
        elapsed = time.time() - start
        if ok:
            print(f"✅ 성공 ({elapsed:.1f}s)")
        else:
            print(f"❌ 실패 (반환값 False, {elapsed:.1f}s)")
            return False
    except Exception as e:
        elapsed = time.time() - start
        print(f"❌ 에러 ({elapsed:.1f}s)")
        print(f"    {e}")
        traceback.print_exc()
        return False

    # 2) 검색
    print(f"  검색 '{TEST_KEYWORD}'... ", end="", flush=True)
    start = time.time()
    try:
        results = crawler.search(TEST_KEYWORD)
        elapsed = time.time() - start
        print(f"✅ {len(results)}건 ({elapsed:.1f}s)")
        for r in results[:3]:
            print(f"    - {r.product_name} | {r.unit} | {r.price}원 | 재고:{r.quantity} | ID:{r.product_id}")
        if len(results) > 3:
            print(f"    ... 외 {len(results) - 3}건")
        return True
    except Exception as e:
        elapsed = time.time() - start
        print(f"❌ 에러 ({elapsed:.1f}s)")
        print(f"    {e}")
        traceback.print_exc()
        return False


def main():
    # registry.py import 시 _register_all() 자동 실행됨

    targets = sys.argv[1:] if len(sys.argv) > 1 else list(SEED_CREDENTIALS.keys())

    print(f"=== 도매 크롤러 테스트 (키워드: {TEST_KEYWORD}) ===\n")

    results = {}
    for name in targets:
        print(f"[{name}]")
        results[name] = test_crawler(name)
        print()

    # 요약
    print("=== 결과 요약 ===")
    for name, ok in results.items():
        status = "✅ 성공" if ok else "❌ 실패"
        print(f"  {name}: {status}")


if __name__ == "__main__":
    main()
