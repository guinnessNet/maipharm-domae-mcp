"""HMP 크롤러

특이사항:
- DWR(Direct Web Remoting) 프로토콜 기반
- 주문 기능 미구현
"""

import re
import json
from typing import List

from bs4 import BeautifulSoup as bs

from domae_mcp.core.crawlers.base import BaseCrawler, SearchResult, OrderResult


class HmpMallCrawler(BaseCrawler):
    """HMP 도매 크롤러 (DWR 프로토콜)."""

    SUPPLIER_NAME = "HMP"

    def login(self, login_id: str, login_pw: str) -> bool:
        headers = {
            "Content-Type": "text/plain",
            "Origin": "https://www.hmpmall.co.kr",
            "Referer": "https://www.hmpmall.co.kr/login.do",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
        }

        # DWR 프로토콜 로그인
        data = (
            "callCount=1\n"
            "nextReverseAjaxIndex=0\n"
            "c0-scriptName=common/Login\n"
            "c0-methodName=execute\n"
            "c0-id=0\n"
            "c0-e1=string:\n"
            "c0-e2=string:2350001\n"
            f"c0-e3=string:{login_id}\n"
            f"c0-e4=string:{login_pw}\n"
            "c0-param0=Object_Object:{mallDivCode:reference:c0-e1, loginPathDivCode:reference:c0-e2, memId:reference:c0-e3, memPw:reference:c0-e4}\n"
            "batchId=0\n"
            "instanceId=0\n"
            "page=%2Flogin.do\n"
            "scriptSessionId=local/5m9LVKo-1FYR59k9p\n"
        )

        resp = self.session.post(
            "https://www.hmpmall.co.kr/dwr/call/plaincall/common/Login.execute.dwr",
            headers=headers,
            data=data,
        )
        self.session.get("https://www.hmpmall.co.kr/home.do")
        self._logged_in = True
        return resp.status_code == 200

    def _search_second_step(self, product_master_id: int) -> dict:
        """개별 제품 상세 정보 (판매자별 재고/가격)"""
        headers = {
            "X-Requested-With": "XMLHttpRequest",
            "Referer": "https://www.hmpmall.co.kr/search/searchTwoStepList.do",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
        }
        params = {
            "productMasterId": product_master_id,
            "fromGubun": "2480000",
            "orderByProductSeller": "",
            "orderbySellerId": "",
            "preProductMasterId": "19257",
            "groupCategoryNumber": "1",
            "imgPath": "/statics/imgs",
            "imgPathX": "/statics/imgs",
        }

        resp = self.session.get(
            "https://www.hmpmall.co.kr/search/SearchProductSellerListJson.do",
            params=params,
            headers=headers,
        )
        data = json.loads(resp.text)

        stock_qty = 0
        price = None
        for seller in data.get("sellerSaleProductList", []):
            stock_qty += int(seller.get("stockQuantity", 0))
            try:
                seller_price = seller.get("minProductUnitPrice", 0)
                if price is None or (seller_price < price and int(seller.get("stockQuantity", 0)) != 0):
                    price = seller_price
            except (TypeError, ValueError):
                pass

        basic_info = data.get("productBasicInfo", {})
        insurance_code = basic_info.get("insuranceCode", "")

        return {
            "maker": basic_info.get("manufacturerName", ""),
            "product_name": basic_info.get("productName", ""),
            "unit": basic_info.get("packingUnit", ""),
            "insurance_code": insurance_code or "",
            "quantity": stock_qty,
            "price": price or 0,
        }

    def search(self, keyword: str) -> list[SearchResult]:
        # ensure_login은 외부에서 login_id/login_pw를 전달받아 호출해야 함
        # search 단독 호출 시에는 이미 로그인된 상태라고 가정
        results = []

        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": "https://www.hmpmall.co.kr",
            "Referer": "https://www.hmpmall.co.kr/home.do",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
        }
        data = {
            "sellerId": "", "productMasterId": "",
            "productName": keyword, "manufacturerName": "",
            "ingredient": "", "dracClsfName": "",
            "insuranceCode": "", "groupCategoryNumber": "",
            "largeCategoryNumber": "", "middleCategoryNumber": "",
            "smallCategoryNumber": "", "orderFieldName": "",
            "orderType": "", "autoCompleteSelectYn": "",
            "searchKeyword": keyword, "headerSearchKeyword": keyword,
            "skip": "1", "max": "20", "yuhaTest": "false",
            "makingId": "productName", "paramSellerId": "",
            "sellerIdList": "", "productNameList": "",
            "manufacturerNameList": "", "ingredientList": "",
            "categoryTreeSearch": "", "categoryTreeSearchLevel": "",
            "preWhereSetStr": "", "fromGubun": "",
            "preProductMasterId": "", "delvBpcoDivCd": "",
            "pageDtlNum": "", "hanmiProductIdx": "",
            "treatmentDeptNumber": "", "promotionKindDivCode": "",
            "menuId": "", "subMenuId": "",
        }

        resp = self.session.post(
            "https://www.hmpmall.co.kr/search/searchTwoStepList.do",
            headers=headers,
            data=data,
        )

        soup = bs(resp.text, "html.parser")

        # 제품 ID 추출
        pattern = r"searchTwoStepList\.searchSubList\(\s*'\d','\d{5,6}'"
        product_ids = set()
        for a_tag in soup.find_all("a"):
            href = str(a_tag.get("href", ""))
            if re.search(pattern, href):
                match = re.search(r"\d{5,6}", href)
                if match:
                    product_ids.add(int(match.group()))

        # 개별 제품 상세 조회
        for pid in product_ids:
            try:
                info = self._search_second_step(pid)
            except Exception:
                continue

            results.append(SearchResult(
                maker=info["maker"],
                product_name=info["product_name"],
                unit=info["unit"],
                insurance_code=info["insurance_code"],
                quantity=info["quantity"],
                supplier=self.SUPPLIER_NAME,
                price=info["price"],
            ))

        return results

    def order(self, product_id: str, quantity: int) -> OrderResult:
        return OrderResult(success=False, message="미구현")
