"""지오영 크롤러

기본 패턴의 도매상 크롤러.
로그인 → 세션 쿠키 유지 → 검색/주문 수행.
"""

from domae_mcp.core.crawlers.base import (
    BaseCrawler,
    CrawlerError,
    OrderResult,
    SearchResult,
)

# TODO: 실제 도메인으로 교체
BASE_URL = "https://www.geoweb.co.kr"
LOGIN_URL = f"{BASE_URL}/member/login_proc.asp"
SEARCH_URL = f"{BASE_URL}/product/search.asp"
ORDER_URL = f"{BASE_URL}/order/order_proc.asp"


class GeoWebCrawler(BaseCrawler):
    """지오영 도매 크롤러.

    - 표준 form POST 로그인
    - 검색: GET 파라미터로 키워드 전달, HTML 테이블 파싱
    - 주문: POST로 product_id + 수량 전달
    """

    SUPPLIER_NAME = "지오영"

    def login(self, login_id: str, login_pw: str) -> bool:
        """지오영 로그인.

        POST form 데이터로 아이디/비밀번호 전송.
        성공 시 세션 쿠키가 유지됨.
        """
        try:
            # TODO: 실제 form 필드명 확인 필요
            payload = {
                "user_id": login_id,
                "user_pw": login_pw,
            }
            resp = self.session.post(LOGIN_URL, data=payload)
            resp.raise_for_status()

            # TODO: 로그인 성공 판별 로직 (리다이렉트 URL, 응답 내 특정 문자열 등)
            # 예: "로그아웃" 텍스트가 응답에 있으면 성공
            if "logout" in resp.text.lower() or "로그아웃" in resp.text:
                self._logged_in = True
                return True

            return False
        except Exception as e:
            raise CrawlerError(f"[지오영] 로그인 실패: {e}") from e

    def search(self, keyword: str) -> list[SearchResult]:
        """지오영 검색.

        GET 요청으로 키워드 전달 → HTML 테이블에서 결과 파싱.
        """
        try:
            # TODO: 실제 파라미터명 확인 필요
            params = {"search_word": keyword}
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
                    # TODO: product_id 추출 방식 확인 (hidden input, data 속성 등)
                    product_id=row.get("data-id", ""),
                ))

            return results
        except CrawlerError:
            raise
        except Exception as e:
            raise CrawlerError(f"[지오영] 검색 실패: {e}") from e

    def order(self, product_id: str, quantity: int) -> OrderResult:
        """지오영 주문.

        POST로 상품 ID와 수량을 전달하여 주문 실행.
        """
        try:
            # TODO: 실제 form 필드명 확인 필요
            payload = {
                "product_id": product_id,
                "qty": str(quantity),
            }
            resp = self.session.post(ORDER_URL, data=payload)
            resp.raise_for_status()

            # TODO: 주문 성공 판별 로직
            if "주문완료" in resp.text or "success" in resp.text.lower():
                return OrderResult(
                    success=True,
                    message="주문 성공",
                    order_id="",  # TODO: 응답에서 주문번호 파싱
                )

            return OrderResult(success=False, message="주문 실패: 응답 확인 필요")
        except CrawlerError:
            raise
        except Exception as e:
            raise CrawlerError(f"[지오영] 주문 실패: {e}") from e
