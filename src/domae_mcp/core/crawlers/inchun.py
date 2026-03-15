"""인천 크롤러 (NicePharm 패턴)

NicePharm 기반 도매상. 복산과 동일한 플랫폼 구조.
장바구니 조회는 Bag.asp 엔드포인트 사용.
"""

from domae_mcp.core.crawlers.base import (
    BaseCrawler,
    CrawlerError,
    OrderResult,
    SearchResult,
)

# TODO: 실제 도메인으로 교체
BASE_URL = "https://www.inchunpharm.co.kr"
LOGIN_URL = f"{BASE_URL}/Member/Login_proc.asp"
SEARCH_URL = f"{BASE_URL}/Product/Search.asp"
ORDER_URL = f"{BASE_URL}/Order/Order_proc.asp"
CART_URL = f"{BASE_URL}/Order/Bag.asp"


class InchunCrawler(BaseCrawler):
    """인천 도매 크롤러 (NicePharm 패턴).

    복산과 동일한 NicePharm 플랫폼 사용.
    - 로그인, 검색, 주문 구조가 복산과 거의 동일
    - 장바구니: Bag.asp
    - 도메인과 일부 필드명만 상이할 수 있음
    """

    SUPPLIER_NAME = "인천"

    def login(self, login_id: str, login_pw: str) -> bool:
        """인천 로그인 (NicePharm 패턴).

        복산과 동일한 NicePharm 표준 로그인 프로세스.
        """
        try:
            payload = {
                "MEM_ID": login_id,
                "MEM_PW": login_pw,
            }
            resp = self.session.post(LOGIN_URL, data=payload)
            resp.raise_for_status()

            # TODO: 로그인 성공 판별
            if "logout" in resp.text.lower() or "로그아웃" in resp.text:
                self._logged_in = True
                return True

            return False
        except Exception as e:
            raise CrawlerError(f"[인천] 로그인 실패: {e}") from e

    def search(self, keyword: str) -> list[SearchResult]:
        """인천 검색.

        NicePharm 표준 검색. 복산과 동일한 구조.
        """
        try:
            payload = {
                "SEARCH_WORD": keyword,
            }
            resp = self.session.post(SEARCH_URL, data=payload)
            resp.raise_for_status()

            soup = self._soup(resp.text)
            results: list[SearchResult] = []

            # NicePharm 표준 테이블 구조 (복산과 동일)
            # TODO: 실제 셀렉터 확인 필요
            rows = soup.select("table#productList tr")
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
                    product_id=row.get("data-prd-id", ""),
                ))

            return results
        except CrawlerError:
            raise
        except Exception as e:
            raise CrawlerError(f"[인천] 검색 실패: {e}") from e

    def order(self, product_id: str, quantity: int) -> OrderResult:
        """인천 주문 (장바구니 경유).

        복산과 동일한 NicePharm 장바구니 경유 주문 방식.
        """
        try:
            # 1단계: 장바구니 추가
            cart_payload = {
                "PRD_ID": product_id,
                "QTY": str(quantity),
            }
            resp = self.session.post(f"{BASE_URL}/Order/Bag_insert.asp", data=cart_payload)
            resp.raise_for_status()

            # 2단계: 주문 확정
            order_payload = {
                "PRD_ID": product_id,
            }
            resp = self.session.post(ORDER_URL, data=order_payload)
            resp.raise_for_status()

            # TODO: 주문 성공 판별
            if "주문완료" in resp.text:
                return OrderResult(success=True, message="주문 성공", order_id="")

            return OrderResult(success=False, message="주문 실패: 응답 확인 필요")
        except CrawlerError:
            raise
        except Exception as e:
            raise CrawlerError(f"[인천] 주문 실패: {e}") from e

    def get_cart(self) -> list[dict]:
        """인천 장바구니 조회 (Bag.asp).

        복산과 동일한 NicePharm Bag.asp 패턴.
        """
        try:
            resp = self.session.get(CART_URL)
            resp.raise_for_status()

            soup = self._soup(resp.text)
            items: list[dict] = []

            # TODO: 실제 장바구니 테이블 셀렉터 확인
            rows = soup.select("table.bag-list tr")
            for row in rows:
                cols = row.select("td")
                if len(cols) < 4:
                    continue

                items.append({
                    "product_name": cols[0].get_text(strip=True),
                    "quantity": self._safe_int(cols[1].get_text(strip=True)),
                    "price": self._safe_int(cols[2].get_text(strip=True)),
                    "product_id": row.get("data-prd-id", ""),
                })

            return items
        except Exception as e:
            raise CrawlerError(f"[인천] 장바구니 조회 실패: {e}") from e
