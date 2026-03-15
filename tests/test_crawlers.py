"""
도매 크롤러 통합 테스트 (domae-v2 시드 계정 사용)
Usage: python -m tests.test_crawlers [도매상명]

크롤러 파일은 서버에서 배포되지만, 로컬 개발 시에는
src/domae_mcp/core/crawlers/ 디렉토리에 .py 파일이 존재한다.
이 테스트는 로컬 크롤러 파일을 직접 import하여 테스트한다.
"""
import importlib
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, "src")

from domae_mcp.core.crawlers.base import BaseCrawler

# 크리덴셜은 로컬 설정 파일에서 로드 (공개 레포에 노출 금지)
# ~/.maipharm-domae-mcp/config.json의 credentials 또는
# tests/.test_credentials.json (gitignore 대상) 사용
def _load_test_credentials() -> dict:
    """테스트용 크리덴셜 로드. 로컬 설정 파일 → 테스트 전용 파일 순."""
    # 1) ConfigManager에서 로드 시도
    try:
        from domae_mcp.local.config import ConfigManager, SUPPLIERS
        config = ConfigManager()
        creds = {}
        for supplier in SUPPLIERS:
            c = config.get_credentials(supplier)
            if c.get("login_id") and c.get("login_pw"):
                creds[supplier] = c
        if creds:
            return creds
    except Exception:
        pass

    # 2) tests/.test_credentials.json 에서 로드
    cred_file = Path(__file__).parent / ".test_credentials.json"
    if cred_file.exists():
        import json
        return json.loads(cred_file.read_text(encoding="utf-8"))

    print("⚠️  테스트 크리덴셜 없음. 다음 중 하나를 설정하세요:")
    print("   1) ~/.maipharm-domae-mcp/config.json에 도매 계정 등록")
    print("   2) tests/.test_credentials.json 파일 생성")
    print('      예: {"지오영": {"login_id": "xxx", "login_pw": "yyy"}, ...}')
    return {}


SEED_CREDENTIALS = _load_test_credentials()

# 크롤러 모듈명 → 도매상명 매핑
CRAWLER_MODULES = {
    "지오영": "geoweb",
    "복산": "boksan",
    "인천": "inchun",
    "티제이팜": "tjpharm",
    "HMP": "hmpmall",
    "백제": "beakje",
    "피코": "picomall",
    "새로팜": "saeropharm",
    "신덕팜": "sdpharm",
    "대전동원약품": "upharmmall",
}

TEST_KEYWORD = "아목시실린"


def load_crawler(name: str) -> BaseCrawler | None:
    """로컬 크롤러 파일에서 직접 크롤러 인스턴스 생성."""
    module_name = CRAWLER_MODULES.get(name)
    if not module_name:
        print(f"  ❌ 모듈 매핑 없음: {name}")
        return None

    crawler_path = Path("src/domae_mcp/core/crawlers") / f"{module_name}.py"
    if not crawler_path.exists():
        print(f"  ❌ 크롤러 파일 없음: {crawler_path}")
        return None

    try:
        spec = importlib.util.spec_from_file_location(
            f"domae_mcp.core.crawlers.{module_name}", str(crawler_path)
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        # BaseCrawler 서브클래스 찾기
        for attr_name in dir(module):
            attr = getattr(module, attr_name)
            if (
                isinstance(attr, type)
                and issubclass(attr, BaseCrawler)
                and attr is not BaseCrawler
            ):
                return attr()

        print(f"  ❌ BaseCrawler 서브클래스를 찾을 수 없음")
        return None
    except Exception as e:
        print(f"  ❌ 모듈 로드 실패: {e}")
        traceback.print_exc()
        return None


def test_crawler(name: str):
    """단일 크롤러 로그인 + 검색 테스트"""
    cred = SEED_CREDENTIALS.get(name)
    if not cred:
        print(f"  ❌ 계정 정보 없음")
        return False

    crawler = load_crawler(name)
    if not crawler:
        return False

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
