"""훼미리팜 크롤러

family-pharm.co.kr — JSP 기반 도매몰.
로그인 → 세션 쿠키 유지 → 검색(HTML 파싱).
장바구니: order_send_cart.jsp (JSON), 주문: order_process.jsp
"""

import json
import re
import urllib3
from bs4 import BeautifulSoup as bs
from domae_mcp.core.crawlers.base import BaseCrawler, SearchResult, OrderResult

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BASE_URL = "http://family-pharm.co.kr"
LOGIN_URL = f"{BASE_URL}/member/LoginProcess.jsp"
SEARCH_URL = f"{BASE_URL}/order/order_search.jsp"
CART_ADD_URL = f"{BASE_URL}/order/order_send_cart.jsp"
CART_DELETE_URL = f"{BASE_URL}/order/order_delete.jsp"
ORDER_URL = f"{BASE_URL}/order/order_process.jsp"


class FamilypharmCrawler(BaseCrawler):
    SUPPLIER_NAME = "훼미리팜"

    def login(self, login_id: str, login_pw: str) -> bool:
        self._login_id = login_id
        self._login_pw = login_pw

        login_data = {
            "user_id": login_id,
            "user_pwd": login_pw,
            "member_type": "1",
        }

        resp = self.session.post(LOGIN_URL, data=login_data, verify=False)
        if resp.status_code == 200 and "logout" in resp.text.lower():
            self._logged_in = True
            return True
        return False

    def search(self, keyword: str) -> list[SearchResult]:
        self.ensure_login(self._login_id, self._login_pw)
        results = []

        search_data = {
            "selkeyword": "goods_nm",
            "keywordtext": keyword,
            "prodnm": "",
        }

        resp = self.session.post(SEARCH_URL, data=search_data, verify=False)
        if resp.status_code != 200:
            return results

        resp.encoding = "utf-8"
        soup = bs(resp.text, "html.parser")

        div = soup.find("div", class_="listtable")
        if not div:
            return results

        table = div.find("table")
        if not table:
            return results

        tbody = table.find("tbody")
        rows = tbody.find_all("tr") if tbody else table.find_all("tr")
        for row in rows:
            tds = row.find_all("td")
            if len(tds) < 8:
                continue

            # product_id: goodscd attribute on img or input.qtyctr
            product_id = ""
            fav_img = tds[0].find("img")
            if fav_img:
                product_id = fav_img.get("goodscd", "")

            if not product_id:
                qty_input = tds[7].find("input")
                if qty_input:
                    product_id = qty_input.get("goodscd", "")

            # 보험코드
            insurance_code = tds[1].get_text(strip=True)

            # 제조원 (번호 접두사 제거: "1.일동제약" → "일동제약")
            maker_raw = tds[2].get_text(strip=True)
            maker = re.sub(r"^\d+\.", "", maker_raw).strip()

            # 품명 + 규격: onclick에서 파싱하거나 셀 텍스트에서 분리
            product_name = ""
            unit = ""
            onclick = row.get("onclick", "")
            m = re.search(
                r"goodsDetailView\(\s*'[^']*'\s*,\s*'([^']*)'\s*,\s*'([^']*)'\s*,",
                onclick,
            )
            if m:
                product_name = m.group(1)
                unit = m.group(2)
            else:
                product_name = tds[3].get_text(strip=True)

            # 단가
            price_str = tds[5].get_text(strip=True)

            # 재고
            stock_str = tds[6].get_text(strip=True)

            # 단가 (숫자) — input.qtyctr의 goodsprc 속성에서도 추출 가능
            price = self._safe_int(price_str)
            if price == 0:
                qty_input = tds[7].find("input")
                if qty_input:
                    price = self._safe_int(qty_input.get("goodsprc", "0"))

            results.append(SearchResult(
                maker=maker,
                product_name=product_name,
                unit=unit,
                insurance_code=insurance_code,
                quantity=self._safe_int(stock_str),
                price=price,
                supplier=self.SUPPLIER_NAME,
                product_id=product_id,
            ))

        return results

    # ── 장바구니 헬퍼 ──

    def _get_cart_items(self) -> list[dict]:
        """장바구니 조회 (order_send_cart.jsp 응답 파싱).

        장바구니에 빈 데이터를 보내면 현재 목록이 JSON으로 반환됨.
        """
        resp = self.session.post(
            CART_ADD_URL,
            data={"jsonstr": json.dumps([])},
            verify=False,
        )
        if resp.status_code != 200:
            return []
        try:
            items = resp.json()
            return [
                {"goodscd": item["goodscd"], "qty": item["qty"]}
                for item in items
            ]
        except (json.JSONDecodeError, KeyError):
            return []

    def _clear_cart(self, items: list[dict]):
        """장바구니에서 지정 품목 삭제"""
        if not items:
            return
        delete_data = [{"cd": item["goodscd"]} for item in items]
        self.session.post(
            CART_DELETE_URL,
            data={"jsonstr": json.dumps(delete_data)},
            verify=False,
        )

    def _add_to_cart(self, product_id: str, quantity: int, price: int = 0):
        """장바구니에 제품 담기"""
        cart_data = [{"cd": product_id, "qty": str(quantity), "prc": str(price)}]
        self.session.post(
            CART_ADD_URL,
            data={"jsonstr": json.dumps(cart_data)},
            verify=False,
        )

    def _restore_cart(self, items: list[dict]):
        """저장된 장바구니 품목 복원"""
        for item in items:
            self._add_to_cart(item["goodscd"], int(item["qty"]))

    def _submit_order(self, product_id: str) -> bool:
        """주문 전송"""
        order_data = [{"cd": product_id}]
        resp = self.session.post(
            ORDER_URL,
            data={
                "dataStr": json.dumps(order_data),
                "reqDesc": "",
            },
            verify=False,
        )
        return resp.status_code == 200

    # ── 공개 API ──

    def get_cart(self) -> list[dict]:
        """장바구니 조회"""
        self.ensure_login(self._login_id, self._login_pw)
        items = self._get_cart_items()
        return [
            {"product_id": i["goodscd"], "quantity": int(i["qty"])}
            for i in items
        ]

    def order(self, product_id: str, quantity: int) -> OrderResult:
        """주문: 기존장바구니 캐싱 → 비우기 → 담기 → 전송 → 복원"""
        self.ensure_login(self._login_id, self._login_pw)

        saved_items = []
        try:
            # 1. 기존 장바구니 캐싱
            saved_items = self._get_cart_items()

            # 2. 장바구니 비우기
            if saved_items:
                self._clear_cart(saved_items)

            # 3. 주문할 제품 담기
            self._add_to_cart(product_id, quantity)

            # 4. 주문 전송
            success = self._submit_order(product_id)

            # 5. 기존 장바구니 복원
            self._restore_cart(saved_items)

            if success:
                return OrderResult(success=True, message="주문 전송 완료")
            else:
                return OrderResult(success=False, message="주문 전송 실패")

        except Exception as e:
            # 에러 시에도 장바구니 복원 시도
            try:
                self._restore_cart(saved_items)
            except Exception:
                pass
            return OrderResult(success=False, message=f"주문 에러: {e}")
