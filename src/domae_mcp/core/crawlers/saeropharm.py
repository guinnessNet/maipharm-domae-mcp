"""새로팜 크롤러

domae-v2에서 이식. 검색만 지원, 주문 미구현.
"""

import re
import json
import base64
from bs4 import BeautifulSoup as bs
from domae_mcp.core.crawlers.base import BaseCrawler, SearchResult, OrderResult


class SaeropharmCrawler(BaseCrawler):
    SUPPLIER_NAME = "새로팜"
    CONFIG_SECTION = "saeropharm"

    BASE_URL = "https://www.saeropharm.com"
    UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36"

    def login(self, login_id: str, login_pw: str) -> bool:
        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "Origin": self.BASE_URL,
            "Referer": f"{self.BASE_URL}/front/login/login.do",
            "User-Agent": self.UA,
        }

        # 비밀번호 base64 인코딩
        pw_bytes = login_pw.encode("utf-8")
        pw_b64 = base64.b64encode(pw_bytes).decode("ascii")

        data = {
            "userId": login_id,
            "userPw": pw_b64,
            "userAgent": "",
            "cookieUserIdChk": "N",
        }

        # 로그인 페이지 먼저 방문 (세션 쿠키 받기)
        self.session.get(
            f"{self.BASE_URL}/front/login/login.do",
            headers={"User-Agent": self.UA},
        )

        resp = self.session.post(
            f"{self.BASE_URL}/front/ajax/login/loginCheckAjaxEncrypt.do",
            headers=headers,
            json=data,
        )

        try:
            result = resp.json()
            if str(result.get("flag")) == "4":
                return_url = result.get("returnUrl", "/w/main.do")
                if not return_url.startswith("http"):
                    return_url = self.BASE_URL + return_url
                self.session.get(return_url, allow_redirects=True)
                return True
        except (json.JSONDecodeError, KeyError):
            pass
        return False

    def _get_detail_stock(self, good_sno: str) -> int:
        """상세 AJAX로 입점업체별 재고 합산"""
        try:
            resp = self.session.post(
                f"{self.BASE_URL}/w/ajax/product/productDealerList.do?goodSno={good_sno}",
                headers={
                    "User-Agent": self.UA,
                    "X-Requested-With": "XMLHttpRequest",
                    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                    "Referer": f"{self.BASE_URL}/w/product/searchProductList.do",
                },
            )
            soup = bs(resp.text, "html.parser")
            total = 0
            for li in soup.select("li[data-goodsno]"):
                soldout_btn = li.select_one("button.badge-item")
                if soldout_btn and "품절" in soldout_btn.get_text():
                    continue
                divs = li.select(".wrap > div")
                if len(divs) > 3:
                    stock_text = divs[3].get_text(strip=True).replace(",", "")
                    try:
                        total += int(stock_text)
                    except ValueError:
                        pass
            return total
        except Exception:
            return 0

    def search(self, keyword: str) -> list[SearchResult]:
        results = []

        resp = self.session.get(
            f"{self.BASE_URL}/w/product/searchProductList.do",
            params={"mainSchValue": keyword},
            headers={"User-Agent": self.UA, "Referer": f"{self.BASE_URL}/w/main.do"},
        )

        soup = bs(resp.text, "html.parser")
        items = soup.select(".prd-item")

        for item in items:
            try:
                # 제품명
                name_el = item.select_one("p.name")
                if not name_el:
                    continue
                product_name = name_el.get_text(strip=True)

                # 단위 (규격)
                unit_el = item.select_one("p.text")
                unit = unit_el.get_text(strip=True) if unit_el else ""

                # 가격
                price = 0
                price_el = item.select_one("p.amount")
                if price_el:
                    price_text = price_el.get_text(strip=True)
                    price_text = re.sub(r"[^\d]", "", price_text.split("~")[0])
                    try:
                        price = int(price_text)
                    except ValueError:
                        pass

                # 상품번호 (data-no)
                product_id = item.get("data-no", "")

                # 재고: AJAX로 입점업체별 재고 합산
                quantity = self._get_detail_stock(product_id) if product_id else 0

                results.append(SearchResult(
                    maker="",
                    product_name=product_name,
                    unit=unit,
                    insurance_code="",
                    quantity=quantity,
                    supplier=self.SUPPLIER_NAME,
                    price=price,
                    product_id=str(product_id),
                ))
            except Exception:
                continue

        return results

    def order(self, product_id: str, quantity: int) -> OrderResult:
        return OrderResult(success=False, message="미구현")
