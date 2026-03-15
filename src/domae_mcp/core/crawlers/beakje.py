"""백제 크롤러

특이사항:
- JWT Bearer 토큰 인증 방식 (세션 쿠키가 아닌 Authorization 헤더)
- product_id 형식: ITEM_CD|ITEM_GB_CD (파이프로 구분된 복합 키)
- API 기반 (HTML 파싱이 아닌 JSON 응답)
"""

from domae_mcp.core.crawlers.base import (
    BaseCrawler,
    CrawlerError,
    OrderResult,
    SearchResult,
)

# TODO: 실제 도메인으로 교체
BASE_URL = "https://www.beakje.co.kr"
LOGIN_URL = f"{BASE_URL}/api/auth/login"
SEARCH_URL = f"{BASE_URL}/api/product/search"
ORDER_URL = f"{BASE_URL}/api/order/create"


class BeakjeCrawler(BaseCrawler):
    """백제 도매 크롤러 (JWT Bearer 인증).

    - 로그인: JSON POST → JWT 토큰 수신 → 이후 요청에 Bearer 헤더 부착
    - 검색: API 호출 → JSON 응답 파싱 (HTML 파싱 불필요)
    - 주문: product_id는 ITEM_CD|ITEM_GB_CD 형식의 복합 키
    """

    SUPPLIER_NAME = "백제"

    def __init__(self):
        super().__init__()
        self._jwt_token: str = ""

    def login(self, login_id: str, login_pw: str) -> bool:
        """백제 로그인 (JWT 방식).

        JSON POST로 인증 → JWT 토큰 수신.
        이후 모든 요청에 Authorization: Bearer {token} 헤더 부착.
        """
        try:
            payload = {
                "userId": login_id,
                "userPw": login_pw,
            }
            headers = {
                "Content-Type": "application/json",
            }
            resp = self.session.post(LOGIN_URL, json=payload, headers=headers)
            resp.raise_for_status()

            data = resp.json()
            # TODO: 실제 응답 필드명 확인
            token = data.get("token") or data.get("accessToken", "")
            if token:
                self._jwt_token = token
                # 세션에 Bearer 헤더 설정
                self.session.headers.update({
                    "Authorization": f"Bearer {self._jwt_token}",
                })
                self._logged_in = True
                return True

            return False
        except Exception as e:
            raise CrawlerError(f"[백제] 로그인 실패: {e}") from e

    def search(self, keyword: str) -> list[SearchResult]:
        """백제 검색 (JSON API).

        Bearer 토큰 인증 후 검색 API 호출.
        응답은 JSON 배열.
        """
        try:
            params = {
                "keyword": keyword,
            }
            resp = self.session.get(SEARCH_URL, params=params)
            resp.raise_for_status()

            data = resp.json()
            results: list[SearchResult] = []

            # TODO: 실제 JSON 응답 구조에 맞게 필드명 수정
            items = data.get("items", data.get("list", []))
            for item in items:
                # product_id: ITEM_CD|ITEM_GB_CD 복합 키 형식
                item_cd = item.get("ITEM_CD", "")
                item_gb_cd = item.get("ITEM_GB_CD", "")
                product_id = f"{item_cd}|{item_gb_cd}"

                results.append(SearchResult(
                    maker=item.get("MAKER", item.get("maker", "")),
                    product_name=item.get("ITEM_NM", item.get("productName", "")),
                    unit=item.get("UNIT", item.get("unit", "")),
                    insurance_code=item.get("INS_CODE", item.get("insuranceCode", "")),
                    quantity=self._safe_int(str(item.get("QTY", item.get("quantity", 0)))),
                    price=self._safe_int(str(item.get("PRICE", item.get("price", 0)))),
                    supplier=self.SUPPLIER_NAME,
                    product_id=product_id,
                ))

            return results
        except CrawlerError:
            raise
        except Exception as e:
            raise CrawlerError(f"[백제] 검색 실패: {e}") from e

    def order(self, product_id: str, quantity: int) -> OrderResult:
        """백제 주문 (JWT 인증 API).

        product_id는 ITEM_CD|ITEM_GB_CD 형식.
        파이프 구분자로 분리하여 각각 전송.
        """
        try:
            # product_id 파싱: "ITEM_CD|ITEM_GB_CD"
            parts = product_id.split("|")
            if len(parts) != 2:
                return OrderResult(
                    success=False,
                    message=f"잘못된 product_id 형식: {product_id} (ITEM_CD|ITEM_GB_CD 필요)",
                )

            item_cd, item_gb_cd = parts

            payload = {
                "ITEM_CD": item_cd,
                "ITEM_GB_CD": item_gb_cd,
                "QTY": quantity,
            }
            headers = {
                "Content-Type": "application/json",
            }
            resp = self.session.post(ORDER_URL, json=payload, headers=headers)
            resp.raise_for_status()

            data = resp.json()
            # TODO: 실제 응답 구조에 맞게 성공 판별
            if data.get("success") or data.get("result") == "OK":
                return OrderResult(
                    success=True,
                    message="주문 성공",
                    order_id=data.get("orderId", data.get("ORDER_NO", "")),
                )

            return OrderResult(
                success=False,
                message=data.get("message", "주문 실패"),
            )
        except CrawlerError:
            raise
        except Exception as e:
            raise CrawlerError(f"[백제] 주문 실패: {e}") from e
