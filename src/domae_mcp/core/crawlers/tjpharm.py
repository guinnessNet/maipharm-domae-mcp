"""티제이팜 크롤러

특이사항:
- login_p=2 파라미터 필수 (2단계 로그인 프로세스)
- 모든 요청에 Referer 헤더 필수
- ItemToken을 검색 시 캐싱하여 주문 시 재사용
"""

from domae_mcp.core.crawlers.base import (
    BaseCrawler,
    CrawlerError,
    OrderResult,
    SearchResult,
)

# TODO: 실제 도메인으로 교체
BASE_URL = "https://www.tjpharm.co.kr"
LOGIN_URL = f"{BASE_URL}/member/login_proc.asp"
SEARCH_URL = f"{BASE_URL}/product/search.asp"
ORDER_URL = f"{BASE_URL}/order/order_proc.asp"


class TjPharmCrawler(BaseCrawler):
    """티제이팜 도매 크롤러.

    - login_p=2: 로그인 시 2단계 프로세스 필요
    - Referer 헤더: 모든 요청에 BASE_URL 기반 Referer 필수
    - ItemToken: 검색 결과에 포함된 토큰을 캐싱하여 주문 시 사용
    """

    SUPPLIER_NAME = "티제이팜"

    def __init__(self):
        super().__init__()
        # ItemToken 캐시: {product_id: token}
        self._item_tokens: dict[str, str] = {}
        # Referer 헤더 기본 설정
        self.session.headers.update({
            "Referer": BASE_URL,
        })

    def login(self, login_id: str, login_pw: str) -> bool:
        """티제이팜 로그인 (2단계).

        login_p=2 파라미터를 포함하여 로그인 요청.
        Referer 헤더 필수.
        """
        try:
            # login_p=2: 티제이팜 고유의 2단계 로그인 파라미터
            payload = {
                "user_id": login_id,
                "user_pw": login_pw,
                "login_p": "2",  # 2단계 로그인 플래그
            }
            headers = {
                "Referer": f"{BASE_URL}/member/login.asp",
            }
            resp = self.session.post(LOGIN_URL, data=payload, headers=headers)
            resp.raise_for_status()

            # TODO: 로그인 성공 판별
            if "logout" in resp.text.lower() or "로그아웃" in resp.text:
                self._logged_in = True
                return True

            return False
        except Exception as e:
            raise CrawlerError(f"[티제이팜] 로그인 실패: {e}") from e

    def search(self, keyword: str) -> list[SearchResult]:
        """티제이팜 검색.

        검색 결과에서 ItemToken을 추출하여 캐싱.
        주문 시 해당 토큰이 필요함.
        """
        try:
            params = {
                "search_word": keyword,
            }
            headers = {
                "Referer": f"{BASE_URL}/product/search.asp",
            }
            resp = self.session.get(SEARCH_URL, params=params, headers=headers)
            resp.raise_for_status()

            soup = self._soup(resp.text)
            results: list[SearchResult] = []

            # TODO: 실제 HTML 구조에 맞게 셀렉터 수정
            rows = soup.select("table.product-list tr")
            for row in rows:
                cols = row.select("td")
                if len(cols) < 6:
                    continue

                product_id = row.get("data-id", "")

                # ItemToken 캐싱: 주문 시 필요
                # TODO: 실제 토큰 위치 확인 (hidden input, data 속성 등)
                token_input = row.select_one("input[name='ItemToken']")
                if token_input and product_id:
                    self._item_tokens[product_id] = token_input.get("value", "")

                results.append(SearchResult(
                    maker=cols[0].get_text(strip=True),
                    product_name=cols[1].get_text(strip=True),
                    unit=cols[2].get_text(strip=True),
                    insurance_code=cols[3].get_text(strip=True),
                    quantity=self._safe_int(cols[4].get_text(strip=True)),
                    price=self._safe_int(cols[5].get_text(strip=True)),
                    supplier=self.SUPPLIER_NAME,
                    product_id=product_id,
                ))

            return results
        except CrawlerError:
            raise
        except Exception as e:
            raise CrawlerError(f"[티제이팜] 검색 실패: {e}") from e

    def order(self, product_id: str, quantity: int) -> OrderResult:
        """티제이팜 주문.

        캐싱된 ItemToken을 함께 전송해야 주문 가능.
        Referer 헤더 필수.
        """
        try:
            # 캐싱된 ItemToken 확인
            item_token = self._item_tokens.get(product_id, "")
            if not item_token:
                return OrderResult(
                    success=False,
                    message="ItemToken 없음. 먼저 검색을 수행해주세요.",
                )

            payload = {
                "product_id": product_id,
                "qty": str(quantity),
                "ItemToken": item_token,  # 캐싱된 토큰 필수
            }
            headers = {
                "Referer": f"{BASE_URL}/product/search.asp",
            }
            resp = self.session.post(ORDER_URL, data=payload, headers=headers)
            resp.raise_for_status()

            # TODO: 주문 성공 판별
            if "주문완료" in resp.text or "success" in resp.text.lower():
                return OrderResult(success=True, message="주문 성공", order_id="")

            return OrderResult(success=False, message="주문 실패: 응답 확인 필요")
        except CrawlerError:
            raise
        except Exception as e:
            raise CrawlerError(f"[티제이팜] 주문 실패: {e}") from e
