"""피코 크롤러

domae-v2에서 이식. 검색만 지원, 주문 미구현.
"""

import re
from bs4 import BeautifulSoup as bs
from domae_mcp.core.crawlers.base import BaseCrawler, SearchResult, OrderResult


class PicomallCrawler(BaseCrawler):
    SUPPLIER_NAME = "피코"
    CONFIG_SECTION = "picomall"

    BASE_URL = "https://www.picomall.co.kr"
    UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36"

    def login(self, login_id: str, login_pw: str) -> bool:
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": self.BASE_URL,
            "Referer": f"{self.BASE_URL}/member/login.do",
            "User-Agent": self.UA,
        }

        # 로그인 페이지 먼저 방문 (세션 쿠키 받기)
        self.session.get(
            f"{self.BASE_URL}/member/login.do",
            headers={"User-Agent": self.UA},
        )

        # 실제 로그인 폼: pw는 비우고 xpw에 비밀번호 전송
        data = {
            "id": login_id,
            "pw": "",
            "xpw": login_pw,
        }

        resp = self.session.post(
            f"{self.BASE_URL}/member/login_act.do",
            headers=headers,
            data=data,
            allow_redirects=True,
        )
        # 로그인 성공 시 메인 페이지로 리다이렉트
        return "login.do" not in resp.url

    def _get_detail_stock(self, goodsmasterno: str) -> int:
        """상세 AJAX로 공급사별 재고 합산"""
        try:
            resp = self.session.post(
                f"{self.BASE_URL}/goods/search_goods_ajax.do",
                data={"goodsmasterno": goodsmasterno, "page": "1"},
                headers={
                    "User-Agent": self.UA,
                    "X-Requested-With": "XMLHttpRequest",
                    "Referer": f"{self.BASE_URL}/goods/search_goods.do",
                },
            )
            soup = bs(resp.text, "html.parser")
            seller_wrap = soup.select_one("dl.sellerWrap")
            if not seller_wrap:
                return 0

            total = 0
            for dd in seller_wrap.select("dd"):
                inner_dl = dd.select_one("dl")
                if not inner_dl:
                    continue
                cells = inner_dl.select("dd")
                if len(cells) < 3:
                    continue
                stock_cell = cells[2]
                # 입고알림 버튼이 있으면 재고 0
                alarm = stock_cell.select_one("button")
                if alarm and "입고알림" in alarm.get_text():
                    continue
                stock_text = stock_cell.get_text(strip=True).replace(",", "")
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
            f"{self.BASE_URL}/goods/search_goods.do",
            params={"skey": "", "sword": keyword},
            headers={"User-Agent": self.UA, "Referer": f"{self.BASE_URL}/main/index.do"},
        )

        soup = bs(resp.text, "html.parser")
        items = soup.select("ul.exclusive_list > li.exclusive_item")

        for item in items:
            try:
                # 제품명
                title_el = item.select_one("p.title")
                if not title_el:
                    continue
                product_name = title_el.get_text(strip=True)

                # 품절 여부
                is_sold_out = bool(item.select_one(".label00"))

                # 단위 (규격)
                unit_el = item.select("ul > li")
                unit = ""
                maker = ""
                if len(unit_el) >= 3:
                    unit_p = unit_el[2].select_one("p")
                    if unit_p:
                        unit = unit_p.get_text(strip=True)
                    maker_span = unit_el[2].select_one("span")
                    if maker_span:
                        maker_text = maker_span.get_text(strip=True)
                        maker = maker_text.replace("제조사 : ", "").replace("제조사 :", "")

                # 가격
                price = 0
                price_el = item.select_one("strong")
                if price_el:
                    price_text = price_el.get_text(strip=True).replace(",", "")
                    try:
                        price = int(price_text)
                    except ValueError:
                        pass

                # 상품 마스터번호 (product_id)
                master_input = item.select_one("input#goodsmasterno")
                product_id = master_input["value"] if master_input else ""

                # 재고: 상세 AJAX로 공급사별 재고 합산
                if is_sold_out:
                    quantity = 0
                elif product_id:
                    quantity = self._get_detail_stock(product_id)
                else:
                    quantity = 1

                # 검색어가 제품명에 포함된 경우만 반환
                if keyword and keyword not in product_name:
                    continue

                results.append(SearchResult(
                    maker=maker,
                    product_name=product_name,
                    unit=unit,
                    insurance_code="",
                    quantity=quantity,
                    supplier=self.SUPPLIER_NAME,
                    price=price,
                    product_id=product_id,
                ))
            except Exception:
                continue

        return results

    def order(self, product_id: str, quantity: int) -> OrderResult:
        return OrderResult(success=False, message="미구현")
