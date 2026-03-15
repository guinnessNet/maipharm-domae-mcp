"""새로팜 크롤러

검색만 지원하는 도매상.
주문 기능은 미구현 (BaseCrawler 기본 동작 사용).
"""

from domae_mcp.core.crawlers.base import (
    BaseCrawler,
    CrawlerError,
    SearchResult,
)

# TODO: 실제 도메인으로 교체
BASE_URL = "https://www.saeropharm.co.kr"
LOGIN_URL = f"{BASE_URL}/member/login_proc.asp"
SEARCH_URL = f"{BASE_URL}/product/search.asp"


class SaeroPharmCrawler(BaseCrawler):
    """새로팜 도매 크롤러.

    - 검색만 지원, 주문 미구현
    - 표준 form POST 로그인
    - 검색: GET/POST로 키워드 전달, HTML 테이블 파싱
    """

    SUPPLIER_NAME = "새로팜"

    def login(self, login_id: str, login_pw: str) -> bool:
        """새로팜 로그인.

        표준 form POST 로그인.
        """
        try:
            payload = {
                "user_id": login_id,
                "user_pw": login_pw,
            }
            resp = self.session.post(LOGIN_URL, data=payload)
            resp.raise_for_status()

            # TODO: 로그인 성공 판별
            if "logout" in resp.text.lower() or "로그아웃" in resp.text:
                self._logged_in = True
                return True

            return False
        except Exception as e:
            raise CrawlerError(f"[새로팜] 로그인 실패: {e}") from e

    def search(self, keyword: str) -> list[SearchResult]:
        """새로팜 검색.

        GET 요청으로 키워드 전달 → HTML 테이블 파싱.
        """
        try:
            params = {
                "search_word": keyword,
            }
            resp = self.session.get(SEARCH_URL, params=params)
            resp.raise_for_status()

            soup = self._soup(resp.text)
            results: list[SearchResult] = []

            # TODO: 실제 HTML 구조에 맞게 셀렉터 수정
            rows = soup.select("table.product-list tr")
            for row in rows:
                cols = row.select("td")
                if len(cols) < 6:
                    continue

                results.append(SearchResult(
                    maker=cols[0].get_text(strip=True),
                    product_name=cols[1].get_text(strip=True),
                    unit=cols[2].get_text(strip=True),
                    insurance_code=cols[3].get_text(strip=True),
                    quantity=self._safe_int(cols[4].get_text(strip=True)),
                    price=self._safe_int(cols[5].get_text(strip=True)),
                    supplier=self.SUPPLIER_NAME,
                    product_id=row.get("data-id", ""),
                ))

            return results
        except CrawlerError:
            raise
        except Exception as e:
            raise CrawlerError(f"[새로팜] 검색 실패: {e}") from e

    # order()는 미구현 — BaseCrawler 기본 동작(주문 미지원) 사용
