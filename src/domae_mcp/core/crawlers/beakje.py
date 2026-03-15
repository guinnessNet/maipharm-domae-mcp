"""백제 크롤러

특이사항:
- JWT Bearer 토큰 인증 방식
- product_id 형식: ITEM_CD|ITEM_GB_CD (파이프로 구분된 복합 키)
- API 기반 (JSON 응답)
"""

import json
from typing import List

from domae_mcp.core.crawlers.base import BaseCrawler, SearchResult, OrderResult

BASE_URL = "https://www.ibjp.co.kr"


class BeakjeCrawler(BaseCrawler):
    """백제 도매 크롤러 (JWT Bearer 인증)."""

    SUPPLIER_NAME = "백제"

    def __init__(self):
        super().__init__()
        self.jwt_token = None
        self._cust_cd = ""
        self._login_id = ""
        self._login_pw = ""

    def _auth_headers(self) -> dict:
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Origin": "https://ibjp.co.kr",
            "Referer": "https://ibjp.co.kr/",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
        }
        if self.jwt_token:
            headers["Authorization"] = f"Bearer {self.jwt_token}"
        return headers

    def login(self, login_id: str, login_pw: str) -> bool:
        self._login_id = login_id
        self._login_pw = login_pw
        self._cust_cd = login_id

        self.session.get("http://ibjp.co.kr")
        login_data = {
            "companyYn": "N",
            "loginId": login_id,
            "pwd": login_pw,
        }
        resp = self.session.post(f"{BASE_URL}/jwt/login", json=login_data)
        if resp.status_code == 200:
            try:
                data = resp.json()
                self.jwt_token = data.get("token") or data.get("accessToken") or data.get("access_token")
            except (json.JSONDecodeError, AttributeError):
                pass
            self._logged_in = True
            return True
        return False

    def search(self, keyword: str) -> list[SearchResult]:
        self.ensure_login(self._login_id, self._login_pw)
        results = []

        params = {
            "keyword": keyword, "makerNm": "", "history": "N",
            "excludingOutOfOtock": "N", "custCd": self._cust_cd,
            "custGbCd": "01", "ordMakerCd": "", "userGbCd": "30",
            "ing": "N", "eff": "N", "ingno": "AAAAAAAAAAAAAA",
            "effno": "AAAAAAAAAAAA", "searchAll": "Y",
            "professionalYn": "N", "generalYn": "N",
            "paymentYn": "N", "nonPaymentYn": "N", "searchOption": "0",
        }

        resp = self.session.get(f"{BASE_URL}/ord/itemSearch", params=params, headers=self._auth_headers())

        try:
            items = json.loads(resp.text)
        except (json.JSONDecodeError, ValueError):
            return results

        if not isinstance(items, list):
            return results

        # 동일 제품 수량 합산 로직
        seen = {}
        for item in items:
            maker = item.get("MAKER_NM", "")
            name = item.get("ITEM_NM", "")
            unit = item.get("UNIT", "")
            stock = item.get("AVAIL_STOCK", 0)
            insurance_code = item.get("BOHUM_CD", "")
            item_cd = item.get("ITEM_CD", "")
            item_gb_cd = item.get("ITEM_GB_CD", "")

            key = (maker, name, unit)
            if key in seen:
                results[seen[key]].quantity += stock
            else:
                seen[key] = len(results)
                results.append(SearchResult(
                    maker=maker,
                    product_name=name,
                    unit=unit,
                    insurance_code=insurance_code,
                    quantity=stock,
                    supplier=self.SUPPLIER_NAME,
                    price=0,
                    product_id=f"{item_cd}|{item_gb_cd}",
                ))

        return results

    def _get_basket(self) -> list:
        """장바구니 조회"""
        params = {"userGbCd": "30", "custCd": self._cust_cd, "basketGbCd": "01", "gDlvBrchFlag": ""}
        resp = self.session.get(f"{BASE_URL}/ord/basketList", params=params, headers=self._auth_headers())
        try:
            return json.loads(resp.text)
        except (json.JSONDecodeError, ValueError):
            return []

    def _add_to_basket(self, item_cd: str, item_gb_cd: str, quantity: int):
        """장바구니 담기"""
        data = {
            "basketGbCd": "01",
            "saveItemCd": item_cd,
            "saveItemGbCd": item_gb_cd,
            "dlvBrchCd": "",
            "saveItemQty": str(quantity),
            "userId": self._login_id,
            "custCd": self._cust_cd,
        }
        resp = self.session.post(f"{BASE_URL}/ord/addBasket", json=data, headers=self._auth_headers())
        return resp.status_code == 200

    def _delete_from_basket(self, item_cd: str, item_gb_cd: str):
        """장바구니에서 제품 삭제"""
        params = {"saveItemGbCd": item_gb_cd, "saveItemCd": item_cd, "dlvBrchCd": ""}
        self.session.delete(f"{BASE_URL}/ord/deleteComOrdBasket", params=params, headers=self._auth_headers())

    def _submit_order(self, memo: str = "") -> bool:
        """주문 등록"""
        basket = self._get_basket()
        if not basket:
            return False

        # 각 항목에 필수 필드 추가
        for item in basket:
            item["ORD_MEMO"] = memo
            item["BRCH_CD"] = item.get("BRCH_CD", "")
            item["CUST_CD"] = self._cust_cd
            item["DEPT_CD"] = ""
            item["EMP_ID"] = ""
            item["USER_ID"] = self._login_id

        resp = self.session.post(f"{BASE_URL}/ord/orderReg", json=basket, headers=self._auth_headers())
        return resp.status_code == 200

    def order(self, product_id: str, quantity: int) -> OrderResult:
        """백제 주문: 장바구니 캐싱 → 비우기 → 담기 → 전송 → 복원"""
        self.ensure_login(self._login_id, self._login_pw)

        # product_id = "ITEM_CD|ITEM_GB_CD"
        parts = product_id.split("|")
        if len(parts) != 2:
            return OrderResult(success=False, message="잘못된 product_id 형식")
        item_cd, item_gb_cd = parts

        saved_items = []
        try:
            # 1. 기존 장바구니 캐싱
            saved_items = self._get_basket()

            # 2. 기존 장바구니 비우기
            for item in saved_items:
                self._delete_from_basket(item.get("ITEM_CD", ""), item.get("ITEM_GB_CD", ""))

            # 3. 주문 제품 담기
            self._add_to_basket(item_cd, item_gb_cd, quantity)

            # 4. 주문 전송
            success = self._submit_order()

            # 5. 기존 장바구니 복원
            for item in saved_items:
                self._add_to_basket(item.get("ITEM_CD", ""), item.get("ITEM_GB_CD", ""), item.get("ITEM_QTY", 1))

            if success:
                return OrderResult(success=True, message="주문 전송 완료")
            else:
                return OrderResult(success=False, message="주문 전송 실패")

        except Exception as e:
            try:
                for item in saved_items:
                    self._add_to_basket(item.get("ITEM_CD", ""), item.get("ITEM_GB_CD", ""), item.get("ITEM_QTY", 1))
            except Exception:
                pass
            return OrderResult(success=False, message=f"주문 에러: {e}")
