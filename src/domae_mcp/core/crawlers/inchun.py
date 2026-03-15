"""인천 크롤러 (NicePharm 패턴)

domae-v2에서 이식. NicePharm 기반 도매상.
장바구니 조회는 Bag.asp 엔드포인트 사용.
"""

import re
from typing import List
from urllib.parse import quote
from bs4 import BeautifulSoup as bs
from domae_mcp.core.crawlers.base import BaseCrawler, SearchResult, OrderResult

INSURANCE_CODE_PATTERN = re.compile(r"[0-9]{9}")
PC_PATTERN = re.compile(r"pc=(\d+)")

BASE_URL = "https://inchunpharm.com"
ORDER_URL = f"{BASE_URL}/Service/Order/Order.asp"
BAG_URL = f"{BASE_URL}/Service/Order/BagOrder.asp"
BAG_VIEW_URL = f"{BASE_URL}/Service/Order/Bag.asp"
ORDER_END_URL = f"{BASE_URL}/Service/Order/OrderEnd.asp"
VENDOR_CODE = "50R5Z"


class InchunCrawler(BaseCrawler):
    SUPPLIER_NAME = "인천"

    def login(self, login_id: str, login_pw: str) -> bool:
        self._login_id = login_id
        self._login_pw = login_pw
        login_url = f"{BASE_URL}/common/certify/login.asp"
        login_info = {
            "tx_id": login_id,
            "tx_pw": login_pw,
        }
        resp = self.session.post(login_url, data=login_info, verify=False)
        if resp.status_code == 200:
            self._logged_in = True
            return True
        return False

    def search(self, keyword: str) -> list[SearchResult]:
        self.ensure_login(self._login_id, self._login_pw)
        results = []

        if INSURANCE_CODE_PATTERN.match(keyword):
            url = f"{ORDER_URL}?so=0&so2=0&tx_insucd={quote(keyword)}"
        else:
            url = f"{ORDER_URL}?tx_physic={quote(keyword)}"

        resp = self.session.get(url)
        soup = bs(resp.text, "html.parser")

        wrap = soup.find("div", "wrap_table")
        if not wrap:
            return results
        table = wrap.find("table", "tbl_list")
        if not table:
            return results
        tbody = table.find("tbody")
        if not tbody:
            return results

        trs = tbody.find_all("tr", "ln_physic")
        for tr in trs:
            tds = tr.find_all("td")

            try:
                qty = int(tds[6].get_text(strip=True).replace(",", ""))
            except ValueError:
                qty = 0

            try:
                price = int(tds[5].get_text(strip=True).replace(",", ""))
            except ValueError:
                price = 0

            # 제품코드 추출 (링크 href에서 pc=XXXXX)
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
                quantity=qty,
                supplier=self.SUPPLIER_NAME,
                price=price,
                product_id=product_id,
            ))

        return results

    def _get_cart_items(self) -> list:
        """현재 장바구니 목록 파싱 (Bag.asp에서 읽기)"""
        resp = self.session.get(f"{BAG_VIEW_URL}?currVenCd={VENDOR_CODE}")
        soup = bs(resp.text, "html.parser")
        items = []
        idx = 0
        while True:
            pc = soup.find("input", {"name": f"pc_{idx}"})
            qty = soup.find("input", {"name": f"bagQty_{idx}"})
            if not pc:
                break
            items.append({
                "pc": pc.get("value", ""),
                "qty": qty.get("value", "1") if qty else "1",
                "stock": soup.find("input", {"name": f"stock_{idx}"}).get("value", "0") if soup.find("input", {"name": f"stock_{idx}"}) else "0",
                "price": soup.find("input", {"name": f"price_{idx}"}).get("value", "0") if soup.find("input", {"name": f"price_{idx}"}) else "0",
            })
            idx += 1
        return items

    def _clear_cart(self):
        """장바구니 비우기"""
        self.session.get(f"{BAG_URL}?kind=del&currVenCd={VENDOR_CODE}&currMkind=U")

    def _add_to_cart(self, product_id: str, quantity: int, stock: int = 999, price: int = 0):
        """장바구니에 제품 하나 담기"""
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
        """주문 전송 — Bag.asp에서 현재 장바구니 데이터를 읽어서 그대로 전송"""
        resp = self.session.get(f"{BAG_VIEW_URL}?currVenCd={VENDOR_CODE}")
        soup = bs(resp.text, "html.parser")

        # Bag.asp의 form 데이터를 그대로 수집
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
            data["currMkind"] = "U"

        resp = self.session.post(ORDER_END_URL, data=data)
        return resp.status_code == 200

    def order(self, product_id: str, quantity: int) -> OrderResult:
        """인천약품 주문: 기존장바구니 캐싱 → 비우기 → 주문 → 복원"""
        self.ensure_login(self._login_id, self._login_pw)

        saved_items = []
        try:
            # 1. 기존 장바구니 캐싱
            saved_items = self._get_cart_items()

            # 2. 장바구니 비우기
            if saved_items:
                self._clear_cart()

            # 3. 주문할 제품 담기
            self._add_to_cart(product_id, quantity)

            # 4. 주문 전송 (Bag.asp에서 현재 장바구니 읽어서 전송)
            success = self._submit_order()

            # 5. 기존 장바구니 복원
            for item in saved_items:
                self._add_to_cart(item["pc"], int(item["qty"]), int(item["stock"]), int(item["price"]))

            if success:
                return OrderResult(success=True, message="주문 전송 완료")
            else:
                return OrderResult(success=False, message="주문 전송 실패")

        except Exception as e:
            # 에러 발생 시에도 장바구니 복원 시도
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
