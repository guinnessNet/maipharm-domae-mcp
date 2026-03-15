"""티제이팜 크롤러

특이사항:
- login_p=2 파라미터 필수
- 모든 요청에 Referer 헤더 필수
- ItemToken을 검색 시 캐싱하여 주문 시 재사용
"""

import json
from typing import List

from domae_mcp.core.crawlers.base import BaseCrawler, SearchResult, OrderResult

BASE_URL = "https://tjp.co.kr"


class TjPharmCrawler(BaseCrawler):
    """티제이팜 도매 크롤러."""

    SUPPLIER_NAME = "티제이팜"

    def __init__(self):
        super().__init__()
        self._item_tokens: dict[str, str] = {}  # ItemCode -> ItemToken 매핑
        self._login_id = ""
        self._login_pw = ""

    def login(self, login_id: str, login_pw: str) -> bool:
        self._login_id = login_id
        self._login_pw = login_pw
        headers = {"referer": f"{BASE_URL}/login.php?login_p=2"}
        data = {
            "service_gubun": "2",
            "remember_1": "",
            "remember_2": "",
            "userid": login_id,
            "userpwd": login_pw,
        }
        resp = self.session.post(f"{BASE_URL}/login_proc.php", headers=headers, data=data)
        self.session.get(f"{BASE_URL}/Notices/")
        # Order 페이지 방문으로 세션 확정
        self.session.get(f"{BASE_URL}/Order/", headers=headers)
        self._logged_in = True
        return resp.status_code == 200

    def search(self, keyword: str) -> list[SearchResult]:
        self.ensure_login(self._login_id, self._login_pw)
        results = []

        data = {"makerName": "", "name": keyword, "hiCode": ""}
        headers = {"referer": f"{BASE_URL}/Order/"}
        resp = self.session.post(f"{BASE_URL}/Order/item_api.php", data=data, headers=headers)

        try:
            json_data = json.loads(resp.text)
        except (json.JSONDecodeError, ValueError):
            return results

        for item in json_data.get("ResultSet", []):
            try:
                qty = int(item.get("InvQty", 0))
            except (ValueError, TypeError):
                qty = 0
            try:
                price = int(item.get("HiCst", 0))
            except (ValueError, TypeError):
                price = 0

            item_code = str(item.get("ItemCode", ""))
            item_token = item.get("ItemToken", "")
            if item_code and item_token:
                self._item_tokens[item_code] = item_token

            results.append(SearchResult(
                maker=item.get("MkFName", ""),
                product_name=item.get("ItemName", ""),
                unit=item.get("ItemSize", ""),
                insurance_code=item.get("HiCode", ""),
                quantity=qty,
                supplier=self.SUPPLIER_NAME,
                price=price,
                product_id=item_code,
            ))

        return results

    def _get_basket(self) -> list:
        """장바구니 조회"""
        resp = self.session.post(f"{BASE_URL}/Order/basket_api.php")
        try:
            data = json.loads(resp.text)
            return data.get("ResultSet", [])
        except (json.JSONDecodeError, ValueError):
            return []

    def _clear_basket(self):
        """장바구니 전체 삭제"""
        self.session.post(f"{BASE_URL}/Order/basket_del_api.php", data={"itemCode": ""})

    def _add_to_basket(self, item_code: str, price: int, quantity: int, item_token: str = ""):
        """장바구니 담기"""
        data = {
            "ItemCode": item_code,
            "Cst": str(price),
            "Qty": str(quantity),
            "Qty2": "0",
            "ItemToken": item_token,
        }
        resp = self.session.post(f"{BASE_URL}/Order/basket_post_api.php", data=data)
        try:
            result = json.loads(resp.text)
            return result.get("StatusCode") == "OK"
        except (json.JSONDecodeError, ValueError):
            return resp.status_code == 200

    def _submit_order(self, memo: str = "") -> bool:
        """주문 전송"""
        data = {
            "ip": "",
            "memo": memo,
        }
        resp = self.session.post(f"{BASE_URL}/Order/basket_send_api.php", data=data)
        try:
            result = json.loads(resp.text)
            return result.get("StatusCode") == "OK"
        except (json.JSONDecodeError, ValueError):
            return resp.status_code == 200

    def order(self, product_id: str, quantity: int) -> OrderResult:
        """티제이팜 주문: 장바구니 캐싱 → 비우기 → 담기 → 전송 → 복원"""
        self.ensure_login(self._login_id, self._login_pw)

        item_token = self._item_tokens.get(product_id, "")

        saved_items = []
        try:
            # 1. 기존 장바구니 캐싱
            saved_items = self._get_basket()

            # 2. 장바구니 비우기
            if saved_items:
                self._clear_basket()

            # 3. 주문 제품 담기
            add_ok = self._add_to_basket(product_id, 0, quantity, item_token)
            if not add_ok:
                # 복원
                for item in saved_items:
                    self._add_to_basket(item.get("ItemCode", ""), item.get("Cst", 0), item.get("Qty", 1), item.get("ItemToken", ""))
                return OrderResult(success=False, message="장바구니 담기 실패")

            # 4. 주문 전송
            success = self._submit_order()

            # 5. 기존 장바구니 복원
            for item in saved_items:
                self._add_to_basket(item.get("ItemCode", ""), item.get("Cst", 0), item.get("Qty", 1), item.get("ItemToken", ""))

            if success:
                return OrderResult(success=True, message="주문 전송 완료")
            else:
                return OrderResult(success=False, message="주문 전송 실패")

        except Exception as e:
            try:
                for item in saved_items:
                    self._add_to_basket(item.get("ItemCode", ""), item.get("Cst", 0), item.get("Qty", 1), item.get("ItemToken", ""))
            except Exception:
                pass
            return OrderResult(success=False, message=f"주문 에러: {e}")
