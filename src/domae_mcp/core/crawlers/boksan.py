"""복산 크롤러 (NicePharm 패턴)

domae-v2에서 이식. NicePharm 기반 도매상.
장바구니 조회는 Bag.asp 엔드포인트 사용.
"""

import re
from typing import List
from bs4 import BeautifulSoup as bs
from domae_mcp.core.crawlers.base import BaseCrawler, SearchResult, OrderResult

PC_PATTERN = re.compile(r"pc=(\d+)")

BASE_URL = "https://wos.nicepharm.com"
BAG_URL = f"{BASE_URL}/Service/Order/BagOrder.asp"
BAG_VIEW_URL = f"{BASE_URL}/Service/Order/Bag.asp"
ORDER_END_URL = f"{BASE_URL}/Service/Order/OrderEnd.asp"
VENDOR_CODE = "5114580"


class BoksanCrawler(BaseCrawler):
    SUPPLIER_NAME = "복산"

    def __init__(self):
        super().__init__()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/94.0.4606.81 Safari/537.36",
        })

    def login(self, login_id: str, login_pw: str) -> bool:
        self._login_id = login_id
        self._login_pw = login_pw
        login_url = f"{BASE_URL}/Common/Certify/Login.asp"
        login_info = {
            "tx_id": login_id,
            "tx_pw": login_pw,
        }
        headers = {"Referer": f"{BASE_URL}/Contents/Main/Main9.asp"}
        resp = self.session.post(login_url, headers=headers, data=login_info, verify=False)
        if resp.status_code == 200:
            self._logged_in = True
            return True
        return False

    def search(self, keyword: str) -> list[SearchResult]:
        self.ensure_login(self._login_id, self._login_pw)
        results = []

        search_url = f"{BASE_URL}/Service/Order/Order.asp"
        search_info = {
            "saveNumOrders": "",
            "UserDef001": "02",
            "so": "0",
            "so2": "0",
            "tx_ven": VENDOR_CODE,
            "currVenNm": "512091-열린온누리약국",
            "currMkind": "U",
            "sv": VENDOR_CODE,
            "currStockCd": "59003",
            "currSndStockCd": "50003",
            "tx_physic": keyword,
        }
        headers = {"Referer": f"{BASE_URL}/Service/Order/Order.asp"}
        resp = self.session.post(search_url, data=search_info, headers=headers, verify=False)
        soup = bs(resp.text, "html.parser")

        div = soup.find("div", {"class": "wrap_table"})
        if not div:
            return results
        table = div.find("table", {"class": "tbl_list"})
        if not table:
            return results
        tbody = table.find("tbody")
        if not tbody:
            return results

        trs = tbody.find_all("tr", {"class": "ln_physic"})
        for tr in trs:
            tds = tr.find_all("td")

            a = tds[7].get_text(strip=True)
            b = tds[8].get_text(strip=True)
            amount = 0
            if a != "품절":
                amount += int(a.replace(",", ""))
            if b != "품절":
                amount += int(b.replace(",", ""))

            try:
                price = int(tds[5].get_text(strip=True).replace(",", ""))
            except ValueError:
                price = 0

            # 제품코드 추출
            product_id = ""
            link = tds[2].find("a")
            if link and link.get("href"):
                match = PC_PATTERN.search(link["href"])
                if match:
                    product_id = match.group(1)

            results.append(SearchResult(
                maker=tds[1].get_text(strip=True),
                product_name=tds[2].get_text(strip=True),
                unit=tds[3].get_text(strip=True),
                insurance_code=tds[0].get_text(strip=True),
                quantity=amount,
                supplier=self.SUPPLIER_NAME,
                price=price,
                product_id=product_id,
            ))

        return results

    def _get_cart_items(self) -> list:
        resp = self.session.get(f"{BAG_VIEW_URL}?currVenCd={VENDOR_CODE}")
        soup = bs(resp.text, "html.parser")
        items = []
        idx = 0
        while True:
            pc = soup.find("input", {"name": f"pc_{idx}"})
            if not pc:
                break
            qty_el = soup.find("input", {"name": f"bagQty_{idx}"})
            stock_el = soup.find("input", {"name": f"stock_{idx}"})
            price_el = soup.find("input", {"name": f"price_{idx}"})
            items.append({
                "pc": pc.get("value", ""),
                "qty": qty_el.get("value", "1") if qty_el else "1",
                "stock": stock_el.get("value", "0") if stock_el else "0",
                "price": price_el.get("value", "0") if price_el else "0",
            })
            idx += 1
        return items

    def _clear_cart(self):
        self.session.get(f"{BAG_URL}?kind=del&currVenCd={VENDOR_CODE}&currMkind=")

    def _add_to_cart(self, product_id: str, quantity: int, stock: int = 999, price: int = 0):
        data = {
            "qty_0": str(quantity),
            "pc_0": product_id,
            "stock_0": str(stock),
            "saleqty_0": "0",
            "price_0": str(price),
            "selectNumOrder": "10",
            "saveNumOrder": "10",
            "userId": self._login_id,
            "intArray": "0",
            "kind": "saveAll",
            "idx": "-1",
            "currVenCd": VENDOR_CODE,
        }
        self.session.post(BAG_URL, data=data)

    def _submit_order(self):
        """Bag.asp에서 현재 장바구니 데이터를 읽어서 그대로 전송"""
        resp = self.session.get(f"{BAG_VIEW_URL}?currVenCd={VENDOR_CODE}")
        soup = bs(resp.text, "html.parser")
        data = {}
        for inp in soup.find_all("input"):
            name = inp.get("name", "")
            if not name:
                continue
            value = inp.get("value", "")
            if inp.get("type") == "checkbox":
                data[name] = "on"
            else:
                data[name] = value
        data["kind"] = "bag_saveall"
        data["Rz043PhyCd"] = ""
        if "currMkind" not in data:
            data["currMkind"] = ""
        resp = self.session.post(ORDER_END_URL, data=data)
        return resp.status_code == 200

    def order(self, product_id: str, quantity: int) -> OrderResult:
        self.ensure_login(self._login_id, self._login_pw)
        saved_items = []
        try:
            saved_items = self._get_cart_items()
            if saved_items:
                self._clear_cart()
            self._add_to_cart(product_id, quantity)
            success = self._submit_order()
            for item in saved_items:
                self._add_to_cart(item["pc"], int(item["qty"]), int(item["stock"]), int(item["price"]))
            if success:
                return OrderResult(success=True, message="주문 전송 완료")
            else:
                return OrderResult(success=False, message="주문 전송 실패")
        except Exception as e:
            try:
                for item in saved_items:
                    self._add_to_cart(item["pc"], int(item["qty"]), int(item["stock"]), int(item["price"]))
            except Exception:
                pass
            return OrderResult(success=False, message=f"주문 에러: {e}")

    def get_cart(self) -> list[dict]:
        """장바구니 조회 (Bag.asp에서 읽기)"""
        items = self._get_cart_items()
        return [{"product_id": i["pc"], "quantity": int(i["qty"]), "price": int(i["price"])} for i in items]
