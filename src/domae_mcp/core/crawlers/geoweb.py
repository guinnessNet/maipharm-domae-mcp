"""지오영 크롤러

domae-v2에서 이식. 로그인 → 세션 쿠키 유지 → 검색/주문 수행.
"""

import urllib3
from typing import List
from bs4 import BeautifulSoup as bs
from domae_mcp.core.crawlers.base import BaseCrawler, SearchResult, OrderResult

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


class GeoWebCrawler(BaseCrawler):
    SUPPLIER_NAME = "지오영"

    def login(self, login_id: str, login_pw: str) -> bool:
        self._login_id = login_id
        self._login_pw = login_pw
        login_url = "https://order.geoweb.kr/Member/Login"
        login_info = {
            "LoginID": login_id,
            "Password": login_pw,
        }
        resp = self.session.post(login_url, data=login_info, verify=False)
        if resp.status_code == 200:
            self._logged_in = True
            return True
        return False

    def search(self, keyword: str) -> list[SearchResult]:
        self.ensure_login(self._login_id, self._login_pw)
        results = []

        search_url = "https://order.geoweb.kr/Home/PartialSearchProduct"
        ajax_url = "https://order.geoweb.kr/Home/PartialProductInfo/"

        resp = self.session.post(search_url, data={"srchText": keyword}, verify=False)
        if resp.status_code != 200:
            return results

        soup = bs(resp.text, "html.parser")
        trs = soup.find_all("tr")

        for tr in trs:
            tds = tr.find_all("td")
            if not tds:
                continue

            if tds[0].text == "검색된 제품이 없습니다.":
                break

            li_element = tds[6].select_one("li:first-child")
            if not li_element:
                continue

            product_code = li_element.get_text(strip=True)
            resp2 = self.session.post(ajax_url + product_code, data={"num": 0}, verify=False)
            soup2 = bs(resp2.text, "html.parser")

            # 타센터 재고 합산
            other_center = soup2.select_one("div.another_center_board > table > tbody > tr > td:nth-child(2)")
            other_qty = int(other_center.get_text(strip=True).replace(",", "")) if other_center else 0

            price_td = soup2.select_one("table > tbody > tr:nth-child(3) > td")
            price = price_td.get_text(strip=True) if price_td else "0"

            local_qty = int(tds[5].get_text(strip=True).replace(",", ""))
            total_qty = local_qty + other_qty

            try:
                price_int = int(str(price).replace(",", ""))
            except ValueError:
                price_int = 0

            results.append(SearchResult(
                maker=tds[2].get_text(strip=True),
                product_name=tds[3].get_text(strip=True),
                unit=tds[4].get_text(strip=True),
                insurance_code=tds[1].get_text(strip=True),
                quantity=total_qty,
                supplier=self.SUPPLIER_NAME,
                price=price_int,
                product_id=product_code,
            ))

        return results

    def order(self, product_id: str, quantity: int) -> OrderResult:
        """지오영 주문: 장바구니 담기 → 주문 전송"""
        self.ensure_login(self._login_id, self._login_pw)

        # 1. 장바구니 담기
        cart_url = "https://order.geoweb.kr/Home/DataCart/add"
        cart_data = {
            "productCode": product_id,
            "moveCode": "",
            "orderQty": str(quantity),
        }
        resp = self.session.post(cart_url, data=cart_data, verify=False)
        if resp.status_code != 200:
            return OrderResult(success=False, message=f"장바구니 담기 실패 (status: {resp.status_code})")

        # 2. 주문 전송
        order_url = "https://order.geoweb.kr/Home/DataOrder"
        order_data = {"p_desc": ""}
        resp2 = self.session.post(order_url, data=order_data, verify=False)
        if resp2.status_code == 200:
            return OrderResult(success=True, message="주문 전송 완료")
        else:
            return OrderResult(success=False, message=f"주문 전송 실패 (status: {resp2.status_code})")
