# tests/test_geoweb_order.py
"""지오영 — session.get/post 만 대역. 장바구니·주문내역 HTML 은 실측 구조(2026-10-02),
검색 행·결과 없음 행·상세 페이지는 실측 구조(2026-10-04)를 따른다."""
import importlib.util
import os
import sys
sys.path.insert(0, "src")

import fakeredis

from domae_mcp.core.crawlers.cart_snapshot import CartSnapshot

PATH = os.environ.get("GEOWEB_PY", os.path.expanduser(
    "~/.config/superpowers/worktrees/pharmsquare-server-main/order-resilience/prisma/seeds/domae-crawlers/geoweb.py"))
spec = importlib.util.spec_from_file_location("geoweb_under_test", PATH)
gw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gw)


class Resp:
    def __init__(self, text, status=200, url="https://order.geoweb.kr/x"):
        self.text, self.status_code, self.url = text, status, url


LOGIN = Resp("<html>login</html>", 200, "https://order.geoweb.kr/Member/Login?ReturnUrl=%2fMyPage")
# 실측(2026-10-04): 검색 td.proName 은 "제품명 규격 제조사약칭", td.phaCompany 는 정식 제조사명.
# 주문내역 td.proName 도 검색 td.proName 과 같은 문자열이다.
NAMES = {"A": ("가상정A 30T 가상제약", "30T", "가상제약(주)"),
         "B": ("가상정B 30T 나제약", "30T", "나제약(주)"),
         "Z": ("가상정Z 30T 다제약", "30T", "다제약(주)")}
CODES = {"A": "123456789", "B": "223456789", "Z": "323456789"}
EMPTY_SEARCH = '<tr> <td colspan="20">검색된 제품이 없습니다.</td> </tr>'


def _gnorm(s):
    return "".join((s or "").split())


def search_row(pid="A", code=None, stock="9", other="0", name=None, unit=None, maker=None):
    """실측 검색 행(8칸). li[0]=제품코드, li[4]=자기센터 재고."""
    n, u, m = NAMES.get(pid, (pid, "30T", "제조사"))
    name = n if name is None else name
    unit = u if unit is None else unit
    maker = m if maker is None else maker
    code = CODES.get(pid, "") if code is None else code
    return ('<tr class="tr-product-list" style="cursor:pointer;"> <td class="check"> </td>'
            f' <td class="code" style="" > {code} </td> <td class="phaCompany"><span>{maker}</span></td>'
            f' <td class="proName"> {name} </td> <td class="standard ellipsis_text_70">{unit}</td>'
            f' <td class="stock"><span class="font_size_13">{stock}</span></td>'
            f' <td class="stock"> <span style="color: #888888;"> <span class="font_size_13">{other}</span> </span> </td>'
            ' <td class="return"> <button class="btn_basic btn_condition" type="button">조건</button>'
            ' <div class="div-product-detail" style="display:none"> <ul>'
            f' <li>{pid}</li><!--0--> <li>{unit}</li><!--1--> <li>240</li><!--2--> <li>10</li><!--3-->'
            f' <li>{stock}</li><!--4--> <li>1</li><!--5--> </ul> </div> </td> </tr>')


class Site:
    def __init__(self, local, other, cart=None, logged_in=True, delete_works=True, accept=True,
                 lose_reply=False, history_lag=0, corrupt_cart=False, centers=None, storage_override=None):
        self.local, self.other = dict(local), dict(other)
        # centers: {pid: [[센터명, 이동코드, 재고], ...]} — 실측처럼 재고 0 센터도 행으로 나온다.
        self.centers = {p: [list(c) for c in cs] for p, cs in (centers or {}).items()}
        self.storage_override = storage_override        # 주문내역 창고를 다른 이름으로 기록(오접수 재현)
        self.names = {}                                  # pid → 주문내역 제품명(실측 대역용)
        self.cart = dict(cart or {})                         # {(pid, move): qty}
        self.logged_in, self.delete_works, self.accept = logged_in, delete_works, accept
        self.lose_reply, self.history_lag, self.corrupt_cart = lose_reply, history_lag, corrupt_cart
        self.orders, self.sends, self.reads_since_send = [], [], None

    def inject_foreign(self, pid, qty):
        self.orders.append((f"DA2610-9{len(self.orders):06d}", [(NAMES[pid][0], "자기센터(가상)", qty)]))

    def _cart_html(self):
        rows = "".join(
            f'<tr><td><div class="div_cart_detail"><li>{p}</li><li>{m}</li></div>'
            f'<input class="inp_cart_modify" value="{"" if self.corrupt_cart else q}"></td></tr>'
            for (p, m), q in self.cart.items())
        return f'<div class="board_wrap">장바구니<table class="fixTableHead">{rows}</table></div>'

    def _mypage_html(self):
        visible = self.orders
        if self.reads_since_send is not None:
            self.reads_since_send += 1
            if self.reads_since_send <= self.history_lag and self.accept:
                visible = self.orders[:-1]
        body = ""
        for no, rows in visible:
            for name, storage, qty in rows:
                body += (f'<tr id="jsDataTR_x"><td class="orderDate">2026-10-02</td><td class="proName">{name}</td>'
                         f'<td class="storage"><div>{storage}</div></td><td class="quantity">{qty}</td>'
                         f'<td class="release">0</td><td class="state">주문접수</td></tr>')
            body += (f'<tr class="tfoot"><td><span class="title">주문시간</span>2026-10-02 10:00:00</td>'
                     f'<td><span class="title">주문번호</span>{no}</td></tr>')
        return f'<input id="dtpFrom" name="dtpFrom"><table>{body or "<tr><td>주문내역이 없습니다</td></tr>"}</table>'

    def get(self, url, verify=False, **k):
        if "/MyPage" in url:
            return Resp(self._mypage_html()) if self.logged_in else LOGIN
        raise AssertionError(url)

    def post(self, url, data=None, verify=False, **k):
        if url.endswith("PartialProductCart"):
            return Resp(self._cart_html()) if self.logged_in else LOGIN
        if url.endswith("DataCart/del"):
            if self.delete_works:
                self.cart.pop((data["productCode"], data.get("moveCode", "")), None)
            return Resp("ok")
        if url.endswith("DataCart/add"):
            key = (data["productCode"], data.get("moveCode", ""))
            self.cart[key] = self.cart.get(key, 0) + int(data["orderQty"])
            return Resp("ok")
        if url.endswith("DataOrder"):
            self.sends.append(dict(self.cart))
            self.reads_since_send = 0
            if self.accept:
                rows = []
                for (p, m), q in self.cart.items():
                    rows.append((self.names.get(p) or NAMES[p][0], self.storage_override or self._storage(p, m), q))
                    if not m:
                        self.local[p] -= q
                    elif p in self.centers:
                        next(c for c in self.centers[p] if c[1] == m)[2] -= q
                    else:
                        self.other[p] -= q
                self.orders.append((f"DA2610-{len(self.orders):07d}", rows))
                self.cart = {}
            if self.lose_reply:
                raise TimeoutError("응답 유실")
            return Resp("ok")
        if url.endswith("PartialSearchProduct"):
            keyword = (data or {}).get("srchText", "")
            rows = [search_row(p, stock=str(self.local.get(p, 0)), other=str(self._other_total(p)))
                    for p in sorted(set(self.local) | set(self.other) | set(self.centers))
                    if keyword in (p, CODES.get(p)) or (_gnorm(keyword) and _gnorm(keyword) in _gnorm(NAMES[p][0]))]
            return Resp("".join(rows) or EMPTY_SEARCH)
        if "PartialProductInfo" in url:
            # 실측: "재고수량" 칸은 요청 num 을 그대로 돌려준다(자기센터 재고의 근거가 아님).
            p = url.rsplit("/", 1)[1]
            num = (data or {}).get("num", 0)
            centers = self.centers.get(p) or ([["타센터가(가상)", "MV", self.other[p]]] if p in self.other else [])
            # 실측: 팝업은 항상 있고(재고 0 센터도 일반 행), 타센터가 없으면 빈 tbody.
            pop = ('<div class="another_center_pop"><table><tbody>' + "".join(
                f'<tr style="" class=""><td>{n}</td><td>{q}</td><td><div class="amount_group">'
                f'<input data-code="{c}" type="text"></div></td></tr>' for n, c, q in centers)
                + '</tbody></table></div>')
            return Resp('<table><tbody><tr><th>제품비고</th><td></td></tr><tr><th>제품코드</th><td></td></tr>'
                        '<tr><th>주문단가</th><td>1000</td><th>박스입수</th><td></td></tr>'
                        f'<tr><th>재고수량</th><td>{num}</td></tr></tbody></table>{pop}')
        raise AssertionError(url)


def _site_other_total(self, p):
    if p in self.centers:
        return sum(q for _, _, q in self.centers[p] if q > 0)
    return self.other.get(p, 0)


def _site_storage(self, p, m):
    if not m:
        return "자기센터(가상)"
    for n, c, _ in self.centers.get(p, []):
        if c == m:
            return n
    return "타센터가(가상)"


Site._other_total = _site_other_total
Site._storage = _site_storage


def crawler(site):
    c = gw.GeoWebCrawler.__new__(gw.GeoWebCrawler)
    c.session, c._stock_cache, c._login_id, c._login_pw, c._logged_in = site, {}, "", "", True
    c._names, c._history_wait = dict(NAMES), 0
    c._dates = ("2026-10-01", "2026-10-02")
    c.ensure_login = lambda *a: True
    c.login = lambda *a: site.logged_in
    c.cart_snapshot = CartSnapshot(fakeredis.FakeRedis(), "m", "지오영")
    return c

def leftover(c, snap):
    """이전 실행이 남긴 기록(잠금은 이미 풀림)."""
    old = CartSnapshot(c.cart_snapshot._r, "m", c.cart_snapshot._s)
    old.lock()
    old.save(snap)
    old.unlock()



def test_geo_untrusted_cart_sends_nothing():
    site = Site({"A": 9}, {}, cart={("Z", ""): 3}, logged_in=False)
    assert crawler(site)._order_bare("A", 2).reason_code == "not_sent" and site.sends == []


def test_geo_corrupt_cart_row_sends_nothing():
    site = Site({"A": 9}, {}, cart={("Z", ""): 3}, corrupt_cart=True)
    assert crawler(site)._order_bare("A", 2).reason_code == "not_sent" and site.sends == []


def test_geo_clear_failure_sends_nothing():
    site = Site({"A": 9}, {}, cart={("Z", ""): 3}, delete_works=False)
    assert crawler(site)._order_bare("A", 2).reason_code == "not_sent" and site.sends == []


def test_geo_success_by_new_order_number_and_restore():
    site = Site({"A": 9}, {}, cart={("Z", ""): 3})
    c = crawler(site)
    assert c._order_bare("A", 2).success and site.cart == {("Z", ""): 3} and c.cart_snapshot.load() is None


def test_geo_split_two_stages_two_order_numbers():
    site = Site({"A": 3}, {"A": 9})
    r = crawler(site)._order_bare("A", 5)
    assert r.success and len(site.sends) == 2 and len(site.orders) == 2


def test_geo_lost_reply_but_history_shows_accepted():
    site = Site({"A": 9}, {}, lose_reply=True)
    assert crawler(site).order("A", 2, product_name="가상정A").success and len(site.sends) == 1


def test_geo_history_lag_is_unknown_not_resent():
    site = Site({"A": 9}, {}, history_lag=99)
    r = crawler(site).order("A", 2, product_name="가상정A")
    assert r.reason_code == "send_unknown" and len(site.sends) == 1


def test_geo_not_accepted_is_unknown_not_rejected():
    site = Site({"A": 9}, {}, accept=False)
    r = crawler(site).order("A", 2, product_name="가상정A")
    assert r.reason_code == "send_unknown" and len(site.sends) == 1


def test_geo_foreign_order_same_product_is_not_counted():
    site = Site({"A": 9}, {}, accept=False)
    orig = site.post

    def post(url, **kw):
        res = orig(url, **kw)
        if url.endswith("DataOrder"):
            site.inject_foreign("A", 1)
        return res
    site.post = post
    assert crawler(site).order("A", 2, product_name="가상정A").reason_code == "send_unknown"


def test_geoweb_stage2_unknown_keeps_stage1():
    site = Site({"A": 3}, {"A": 9})
    orig = site.post

    def post(url, **kw):
        if url.endswith("DataOrder") and len(site.sends) == 1:
            site.accept = False                     # 2단계는 접수 증거 없음
        return orig(url, **kw)
    site.post = post
    r = crawler(site)._order_bare("A", 5)
    assert r.reason_code == "send_unknown" and r.fulfilled_quantity == 3 and len(site.sends) == 2


def test_geo_batch_stops_after_unknown():
    site = Site({"A": 9, "B": 9}, {}, accept=False)
    res = crawler(site).order_batch([{"product_id": "A", "quantity": 1}, {"product_id": "B", "quantity": 1}])
    assert [r.reason_code for r in res] == ["send_unknown", "not_sent"] and len(site.sends) == 1


def test_geo_name_prefix_collision_is_not_accepted():
    site = Site({"A": 9}, {}, accept=False)
    orig = site.post

    def post(url, **kw):
        res = orig(url, **kw)
        if url.endswith("DataOrder"):
            site.orders.append(("DA2610-8000001", [("가상정A플러스 130T 가상제약", "자기센터(가상)", 2)]))
        return res
    site.post = post
    assert crawler(site).order("A", 2, product_name="가상정A").reason_code == "send_unknown"


def test_geo_cart_row_without_detail_is_untrusted():
    site = Site({"A": 9}, {}, cart={("Z", ""): 3})
    html = site._cart_html
    site._cart_html = lambda: html().replace(
        "</table>", '<tr><td><input class="inp_cart_modify" value="1"></td></tr></table>')
    assert crawler(site)._order_bare("A", 2).reason_code == "not_sent" and site.sends == []


def test_geo_guard_blocks_stage2():
    site = Site({"A": 3}, {"A": 9})
    c = crawler(site)
    calls = []

    def guard():
        calls.append(1)
        if len(calls) == 3:
            raise RuntimeError("소유권 상실")
    c.send_guard = guard
    r = c._order_bare("A", 5)
    assert len(site.sends) == 1 and r.success and r.adjusted_quantity == 3


def test_geo_pending_snapshot_blocks():
    site = Site({"A": 9}, {}, cart={("Y", ""): 1})
    c = crawler(site)
    leftover(c, {("Z", ""): 3})
    r = c._order_bare("A", 2)
    assert r.reason_code == "not_sent" and r.no_retry and site.sends == []


def test_geo_history_timeout_after_send_is_unknown():
    site = Site({"A": 9}, {})
    orig_get = site.get

    def get(url, **kw):
        if "/MyPage" in url and site.reads_since_send is not None:
            raise TimeoutError("주문내역 응답 없음")
        return orig_get(url, **kw)
    site.get = get
    r = crawler(site)._order_bare("A", 2)
    assert r.reason_code == "send_unknown" and len(site.sends) == 1


def test_geo_batch_keeps_first_result_when_second_times_out():
    site = Site({"A": 9, "B": 9}, {})
    orig_get = site.get

    def get(url, **kw):
        if "/MyPage" in url and len(site.sends) == 2:
            raise TimeoutError("주문내역 응답 없음")
        return orig_get(url, **kw)
    site.get = get
    res = crawler(site).order_batch([{"product_id": "A", "quantity": 2}, {"product_id": "B", "quantity": 2}])
    assert res[0].success and res[1].reason_code == "send_unknown" and len(site.sends) == 2


def test_geo_user_item_between_stages_is_kept():
    site = Site({"A": 3}, {"A": 9})
    orig = site.post

    def post(url, **kw):
        res = orig(url, **kw)
        if url.endswith("DataOrder") and len(site.sends) == 1:
            site.cart[("Z", "")] = 5                      # 1단계 접수 직후 약사가 Z 를 담음
        return res
    site.post = post
    r = crawler(site)._order_bare("A", 5)
    assert site.cart == {("Z", ""): 5} and len(site.sends) == 1 and r.success and r.adjusted_quantity == 3


def test_geo_presend_stock_zero():
    site = Site({"A": 0}, {})
    assert crawler(site)._order_bare("A", 2).reason_code == "stock_zero" and site.sends == []

import pytest


def search_html(pid="A", code="123456789", stock="9", name=None, unit=None, maker=None, other="0"):
    return search_row(pid, code=code, stock=stock, other=other, name=name, unit=unit, maker=maker)


def with_search(site, html=None):
    original = site.post
    def post(url, **kw):
        if url.endswith('PartialSearchProduct'):
            return Resp(html if html is not None else search_html())
        return original(url, **kw)
    site.post = post
    return site


@pytest.mark.parametrize('stock', ['garbage', '-1', '1,2', '1.5', '', '1 000'])
def test_geo_invalid_search_row_stock_is_unavailable(stock):
    site = with_search(Site({'A': 9}, {}), search_html(stock=stock))
    c = crawler(site)
    assert c._get_product_stocks('A') == (None, 0, '')
    assert c._order_bare('A', 2).reason_code == 'not_sent'
    assert site.sends == []


def test_geo_search_sets_exact_metadata_and_presend_insurance():
    site = with_search(Site({'A': 9}, {'A': 4}), search_html(stock='1,234', other='4'))
    c = crawler(site)
    result = c.search('A')[0]
    assert result.local_stock == 1234 and result.other_stock == 4
    assert c._names['A'] == NAMES['A']
    assert c.presend_stock({'product_id': 'A', 'insurance_code': '000000000'}) == 1238
    assert c.presend_stock({'product_id': 'A', 'insurance_code': '123456789'}) == 1238


@pytest.mark.parametrize('metadata', [('', '30T', '가상제약(주)'), ('   ', '30T', '가상제약(주)')])
def test_geo_incomplete_metadata_is_not_sent(metadata):
    site = with_search(Site({'A': 9}, {}), search_html(unit=metadata[1], name=metadata[0], maker=metadata[2]))
    c = crawler(site)
    c._names['A'] = metadata
    assert c._order_bare('A', 2).reason_code == 'not_sent'
    assert site.sends == []


def test_geo_batch_unsent_second_exception_is_not_unknown():
    site = Site({'A': 9, 'B': 9}, {})
    original = site.post
    def post(url, **kw):
        if url.endswith('PartialProductInfo/B'):
            raise RuntimeError('stock lost')
        return original(url, **kw)
    site.post = post
    result = crawler(site).order_batch([{'product_id':'A','quantity':2}, {'product_id':'B','quantity':2}])
    assert result[0].success and result[0].fulfilled_quantity == 2
    assert result[1].reason_code == 'not_sent'
    assert len(site.sends) == 1


def test_geo_stage_cleanup_exception_retains_accepted_quantity():
    site = Site({'A':3}, {'A':9})
    original = site.post
    def post(url, **kw):
        if url.endswith('PartialProductCart') and site.sends:
            raise RuntimeError('cart unavailable')
        return original(url, **kw)
    site.post = post
    result = crawler(site)._order_bare('A',5)
    assert result.success and result.adjusted_quantity == result.fulfilled_quantity == 3
    assert len(site.sends) == 1

@pytest.mark.parametrize('change', ['missing_marker', 'login', 'http', 'orphan', 'invalid_qty', 'empty_name', 'duplicate_footer'])
def test_geo_invalid_history_is_untrusted(change):
    site = Site({'A':9}, {})
    site.inject_foreign('A',2)
    original = site.get
    def get(url, **kw):
        r = original(url, **kw)
        if change == 'missing_marker': r.text = r.text.replace('id="dtpFrom"','id="other"')
        if change == 'login': r.url = LOGIN.url
        if change == 'http': r.status_code = 500
        if change == 'orphan': r.text = r.text.replace('class="tfoot"','class="x"')
        if change == 'invalid_qty': r.text = r.text.replace('quantity">2','quantity">1,2')
        if change == 'empty_name': r.text = r.text.replace('가상정A 30T 가상제약','')
        if change == 'duplicate_footer': r.text = r.text.replace('DA2610-', 'DA2610-12 DA2610-')
        return r
    site.get = get
    with pytest.raises(gw.HistoryReadError): crawler(site)._order_rows(('2026-10-01','2026-10-02'))
    assert site.sends == []


@pytest.mark.parametrize('change', ['http','login','missing_marker','detail_without_quantity','duplicate_key','fraction','zero'])
def test_geo_invalid_cart_is_untrusted(change):
    site = Site({'A':9}, {}, cart={('Z',''):3})
    original = site.post
    def post(url, **kw):
        r = original(url, **kw)
        if url.endswith('PartialProductCart'):
            if change == 'http': r.status_code = 500
            if change == 'login': r.url = LOGIN.url
            if change == 'missing_marker': r.text = r.text.replace('fixTableHead','other')
            if change == 'detail_without_quantity': r.text = r.text.replace('inp_cart_modify','other')
            if change == 'duplicate_key': r.text = r.text.replace('</table>', r.text[r.text.index('<tr>'):r.text.index('</tr>')+5]+'</table>')
            if change == 'fraction': r.text = r.text.replace('value="3"','value="3.0"')
            if change == 'zero': r.text = r.text.replace('value="3"','value="0"')
        return r
    site.post = post
    assert crawler(site)._order_bare('A',2).reason_code == 'not_sent'
    assert site.sends == [] and site.cart == {('Z',''):3}


@pytest.mark.parametrize('boundary', ['delete','add','guard'])
def test_geo_foreign_cart_freezes_each_mutation_boundary(boundary):
    site = Site({'A':9}, {}, cart={('Z',''):3})
    c = crawler(site)
    calls = []
    original = site.post
    def post(url, **kw):
        r = original(url, **kw)
        if url.endswith('DataCart/del') or url.endswith('DataCart/add'):
            calls.append(url.rsplit('/',1)[-1])
        if (boundary == 'delete' and url.endswith('DataCart/del')) or (boundary == 'add' and url.endswith('DataCart/add')):
            site.cart[('B','')] = 4
        return r
    site.post = post
    if boundary == 'guard': c.send_guard = lambda: site.cart.update({('B',''):4})
    r = c._order_bare('A',2)
    assert r.reason_code == 'not_sent' and site.sends == []
    assert site.cart[('B','')] == 4 and c._cart_frozen and c.cart_snapshot.load() is not None
    assert calls == (['del'] if boundary == 'delete' else ['del','add'])


def test_geo_lost_account_lease_before_send_freezes():
    site = Site({'A':9}, {}, cart={('Z',''):3})
    c = crawler(site)
    c.send_guard = lambda: c.cart_snapshot._r.set(c.cart_snapshot.lock_key, 'someone_else')
    r = c._order_bare('A',2)
    assert r.reason_code == 'not_sent' and site.sends == [] and c._cart_frozen
    assert site.cart == {('A',''):2} and c.cart_snapshot.load() is not None


@pytest.mark.parametrize('new_rows', [[('가상정A 30T 가상제약','자기센터(가상)',2), ('가상정B 30T 나제약','자기센터(가상)',1)], [('가상정A 30T 다른제약','자기센터(가상)',2)]])
def test_geo_corrupt_new_order_not_accepted(new_rows):
    site = Site({'A':9}, {}, accept=False)
    original = site.post
    def post(url, **kw):
        r = original(url, **kw)
        if url.endswith('DataOrder'): site.orders.append(('DA2610-1234567',new_rows))
        return r
    site.post = post
    assert crawler(site)._order_bare('A',2).reason_code == 'send_unknown'
    assert len(site.sends) == 1


def test_geo_multiple_new_orders_not_accepted():
    site = Site({'A':9}, {})
    original = site.post
    def post(url, **kw):
        r = original(url, **kw)
        if url.endswith('DataOrder'): site.inject_foreign('A',2)
        return r
    site.post = post
    assert crawler(site)._order_bare('A',2).reason_code == 'send_unknown'
    assert len(site.sends) == 1


def test_geo_history_lag_eventually_accepts_without_resend():
    site = Site({'A':9}, {}, history_lag=3)
    r = crawler(site)._order_bare('A',2)
    assert r.success and site.reads_since_send == 4 and len(site.sends) == 1


def test_geo_batch_split_and_partial_stock():
    site = Site({'A':3,'B':1}, {'A':9,'B':2}, cart={('Z',''):3})
    c = crawler(site)
    r = c.order_batch([{'product_id':'A','quantity':5},{'product_id':'B','quantity':5}])
    assert r[0].success and r[0].fulfilled_quantity == 5
    assert r[1].success and r[1].adjusted_quantity == r[1].fulfilled_quantity == 3
    assert site.sends == [{('A',''):3},{('A','MV'):2},{('B',''):1},{('B','MV'):2}]
    assert site.cart == {('Z',''):3} and len(c._progress) == 2


def test_geo_multi_other_rows_sum_stocked_centers_and_first_stocked_code():
    site = Site({'A': 3}, {}, centers={'A': [['zero', 'raw-zero', 0], ['x', 'raw-first', 1000], ['y', 'raw-second', 5]]})
    assert crawler(site)._get_product_stocks('A') == (3, 1005, 'raw-first')   # 재고 0 센터 코드는 쓰지 않는다


@pytest.mark.parametrize('stock,code', [('1,2','MV'),('3',''),('-1','MV')])
def test_geo_invalid_other_stock_unavailable(stock, code):
    site = Site({'A':3}, {'A':9})
    original = site.post
    def post(url, **kw):
        r = original(url, **kw)
        if 'PartialProductInfo/' in url:
            r.text = r.text.replace('<td>9</td>', '<td>'+stock+'</td>').replace('data-code="MV"','data-code="'+code+'"')
        return r
    site.post = post
    c = crawler(site)
    assert c._get_product_stocks('A') == (3, 0, '')        # 팝업 판독 실패 → 타센터만 확인 불가
    r = c._order_bare('A', 5)
    assert site.sends == [{('A', ''): 3}] and r.success and r.adjusted_quantity == 3
    assert r.shortfall_reason == 'stopped'                  # 재고 부족이 아니라 확인 실패로 미전송

@pytest.mark.parametrize('qty', [True,0,-1,1.5,'2.0','1,2',None])
def test_geo_invalid_request_never_sends(qty):
    site = Site({'A':9}, {})
    r = crawler(site).order('A',qty)
    assert r.reason_code == 'not_sent' and site.sends == []


def test_geo_missing_names_search_exact_pid_before_send():
    site = with_search(Site({'A':9}, {}))
    c = crawler(site)
    c._names = {}
    assert c._order_bare('A',2).success and c._names['A'] == NAMES['A']
    wrong = with_search(Site({'A':9}, {}), search_html(pid='B'))
    c = crawler(wrong)
    c._names = {}
    assert c._order_bare('A',2).reason_code == 'not_sent' and wrong.sends == []


def test_geo_stage2_before_history_failure_retains_stage1_and_batch_continues_safely():
    site = Site({'A':3,'B':9}, {'A':9})
    original = site.get
    history_calls = []
    def get(url, **kw):
        history_calls.append(url)
        if len(history_calls) == 3: raise TimeoutError('stage2 baseline unavailable')
        return original(url, **kw)
    site.get = get
    r = crawler(site).order_batch([{'product_id':'A','quantity':5},{'product_id':'B','quantity':2}])
    assert r[0].success and r[0].adjusted_quantity == r[0].fulfilled_quantity == 3
    assert r[1].success and r[1].fulfilled_quantity == 2
    assert site.sends == [{('A',''):3},{('B',''):2}]
    assert len(set(history_calls)) == 1


def test_geo_exact_name_whitespace_only():
    site = Site({'A':9}, {})
    original = site.post
    def post(url, **kw):
        r = original(url, **kw)
        if url.endswith('DataOrder'):
            no, rows = site.orders[-1]
            site.orders[-1] = (no,[('가상정A\t30T\n가상제약', rows[0][1],rows[0][2])])
        return r
    site.post = post
    assert crawler(site)._order_bare('A',2).success


def test_geo_caller_description_does_not_replace_search_identity():
    for use_wrapper in (False,True):
        site = with_search(Site({'A':9}, {}),search_html(code=''))
        c = crawler(site)
        method = c.order if use_wrapper else c._order_bare
        r = method('A',2, insurance_code='123456789', product_name='가상정A 30T (가상제약)')
        assert r.success and len(site.sends) == 1


def test_geo_presend_exact_product_id_uses_valid_insurance_candidate():
    site = Site({'A':9}, {})
    original = site.post
    keywords = []
    def post(url, **kw):
        if url.endswith('PartialSearchProduct'):
            keywords.append(kw['data']['srchText'])
            return Resp(search_html(pid='B') if len(keywords) == 1 else search_html())
        return original(url, **kw)
    site.post = post
    c = crawler(site)
    assert c.presend_stock({'product_id':'A', 'insurance_code':'123456789', 'product_name':'가상정A 30T'}) == 9
    assert keywords == ['123456789','가상정A30T']
    keywords.clear()
    assert c.presend_stock({'product_id':'A','insurance_code':'１２３４５６７８９','product_name':'가상정A 30T'}) == 9
    assert keywords == ['가상정A30T','가상정A 30T']


@pytest.mark.parametrize('html', [search_html(stock='1,2'), search_html().replace('<li>A</li>', '<li></li>'),
                                  search_html().replace('<td class="stock"><span class="font_size_13">9</span></td>', '')])
def test_geo_search_malformed_rows_not_stock_zero(html):
    site = with_search(Site({'A':9}, {}),html)
    c = crawler(site)
    assert c.search('A') == []                      # 화면 검색: 손상 행만 제외
    assert c.presend_stock({'product_id':'A'}) is None   # 주문: 대상 손상·식별 불가는 주문 안 함


def test_geo_history_fixed_kst_dates():
    from datetime import datetime as real_datetime
    from unittest.mock import patch
    class Clock:
        calls = 0
        @classmethod
        def now(cls, zone):
            cls.calls += 1
            assert zone == gw.KST
            return real_datetime(2026,10,3,23,59,59,tzinfo=zone)
    site = Site({'A':3}, {'A':9})
    urls = []
    original = site.get
    def get(url, **kw):
        urls.append(url)
        return original(url, **kw)
    site.get = get
    with patch.object(gw,'datetime',Clock):
        assert crawler(site)._order_bare('A',5).success
    assert Clock.calls == 1 and len(urls) == 4
    assert all('dtpFrom=2026-10-02&dtpTo=2026-10-03&dateSel=1&categorySel=1&txtitem=' in u for u in urls)

@pytest.mark.parametrize('loss', ['account','claim'])
def test_geo_final_cart_read_ownership_loss_prevents_send(loss):
    site = Site({'A':9}, {})
    c = crawler(site)
    original = site.post
    guarded = []
    revoked = []
    def guard():
        guarded.append(True)
        if revoked and loss == 'claim': raise RuntimeError('claim expired during final read')
    c.send_guard = guard
    def post(url, **kw):
        r = original(url, **kw)
        if url.endswith('PartialProductCart') and guarded and not revoked:
            revoked.append(True)
            if loss == 'account': c.cart_snapshot._r.set(c.cart_snapshot.lock_key,'different_owner')
        return r
    site.post = post
    r = c._order_bare('A',2)
    assert r.reason_code == 'not_sent' and site.sends == []
    assert len(guarded) >= 1

@pytest.mark.parametrize('entrypoint', ['order','_order_bare','order_batch'])
@pytest.mark.parametrize('with_insurance', [True,False])
def test_geo_cold_metadata_uses_full_item_candidates_before_pid(entrypoint, with_insurance):
    site = Site({'A':9}, {})
    original = site.post
    queries = []
    def post(url, **kw):
        if url.endswith('PartialSearchProduct'):
            keyword = kw['data']['srchText']
            queries.append(keyword)
            supported = '123456789' if with_insurance else '가상정A30T'
            return Resp(search_html() if keyword == supported else '<table><tr><td>검색된 제품이 없습니다.</td></tr></table>')
        return original(url, **kw)
    site.post = post
    c = crawler(site)
    c._names = {}
    item = {'product_id':'A','quantity':2,'product_name':'가상정A 30T'}
    if with_insurance:
        item['insurance_code'] = '123456789'
    if entrypoint == 'order_batch':
        r = c.order_batch([item])[0]
    else:
        r = getattr(c,entrypoint)('A',2,**{k:v for k,v in item.items() if k not in ('product_id','quantity')})
    assert r.success and r.fulfilled_quantity == 2
    assert queries == ['123456789' if with_insurance else '가상정A30T']
    assert c._names['A'] == NAMES['A'] and site.sends == [{('A',''):2}]


EMPTY_NOTICE = '<tr><td>검색된 제품이 없습니다.</td></tr>'


def test_geo_exact_empty_notice_does_not_hide_later_product_or_center():
    html = search_html().replace('<table>', '<table>'+EMPTY_NOTICE)
    site = with_search(Site({'A': 9}, {'A': 4}), html)
    original = site.post
    def post(url, **kw):
        response = original(url, **kw)
        if 'PartialProductInfo/' in url:
            response.text = response.text.replace('<div class="another_center_pop"><table><tbody>',
                                                  '<div class="another_center_pop"><table><tbody>'+EMPTY_NOTICE)
        return response
    site.post = post
    c = crawler(site)
    rows = c.search('A')
    assert len(rows) == 1
    row = rows[0]
    assert (row.product_id, row.local_stock, row.other_stock, row.other_move_code, row.quantity) == ('A', 9, 4, 'MV', 13)
    assert c._names['A'] == NAMES['A']
    assert c._order_bare('A', 2).success and site.sends == [{('A', ''): 2}]


@pytest.mark.parametrize('damaged', [
    '<tr><td>검색된 제품이 없습니다.</td><td>bad</td></tr>',
    '<tr><td>잠시 후 다시 시도</td></tr>',
    '<tr><td colspan="20"><b>검색된 제품이 없습니다.</b></td></tr>',
    search_html().replace('<td class="check"> </td>', '<td class="check">검색된 제품이 없습니다.</td>')
                 .replace('<span class="font_size_13">9</span>', '<span class="font_size_13">bad</span>'),
])
def test_geo_notice_text_or_short_cells_do_not_exempt_damaged_real_row(damaged):
    c = crawler(with_search(Site({'A': 9}, {}), damaged))
    assert c.search('A') == []                      # 안내로 면제되지 않고 손상·식별 불가로 제외된다
    assert c.presend_stock({'product_id': 'A'}) is None


@pytest.mark.parametrize('price,expected', [('1200', 1200), ('1,200', 1200), ('1,200원', 0), ('', 0), ('broken-secret-session', 0), ('1,2', 0)])
def test_geo_price_fallback_preserves_stock_identity_and_order(price, expected, caplog):
    site = with_search(Site({'A': 9}, {'A': 4}))
    original = site.post
    def post(url, **kw):
        response = original(url, **kw)
        if 'PartialProductInfo/' in url:
            response.text = response.text.replace('<td>1000</td>', '<td>'+price+'</td>')
        return response
    site.post = post
    c = crawler(site)
    row = c.search('A')[0]
    assert row.price == expected
    assert (row.product_id, row.quantity, row.local_stock, row.other_stock, row.other_move_code) == ('A', 13, 9, 4, 'MV')
    assert c._names['A'] == NAMES['A'] and c._stock_cache['A'] == (9, 4, 'MV')
    if expected == 0:
        assert '가격' in caplog.text and 'broken-secret-session' not in caplog.text
    assert c._order_bare('A', 2).success and site.sends == [{('A', ''): 2}]


# ── 실측 응답 기반 회귀 ─────────────────────────────────────────────────
# 원문·기대값은 공개 워커 레포가 아니라 비공개 서버 레포에 둔다(seed 와 같은 방식으로 경로 지정).
# GEOWEB_LIVE_FIXTURES 를 지정하지 않았고 기본 경로도 없을 때만 이 묶음을 건너뛴다.
# 지정했는데 파일이 없으면 실패한다(회귀 누락을 숨기지 않는다).

import json
from bs4 import BeautifulSoup as _bs

_LIVE_ENV = os.environ.get("GEOWEB_LIVE_FIXTURES")
LIVE = _LIVE_ENV or os.path.expanduser(
    "~/.config/superpowers/worktrees/pharmsquare-server-main/order-resilience/prisma/seeds/domae-crawlers/"
    "fixtures/geoweb_live_20261004")
live = pytest.mark.skipif(not _LIVE_ENV and not os.path.isdir(LIVE),
                          reason="비공개 실측 픽스처 없음(GEOWEB_LIVE_FIXTURES 미지정)")


def _live(name):
    with open(os.path.join(LIVE, name), encoding="utf-8") as f:
        return f.read()


def _expected():
    return json.loads(_live("expected.json"))


class LiveSite(Site):
    """실측 검색·상세 원문 + 실제 사이트처럼 상세 '재고수량' 은 요청 num 을 돌려준다.
    장바구니·주문내역은 Site 대역. 상세 원문이 없는 pid 는 404."""
    def __init__(self, search_file='search_4rows.html', local=None, other=None):
        super().__init__(local or {}, other or {})
        self.body, self.nums = _live(search_file), []

    def post(self, url, data=None, **k):
        if url.endswith('PartialSearchProduct'):
            return Resp(self.body)
        if 'PartialProductInfo/' in url:
            pid = url.rsplit('/', 1)[1]
            self.nums.append((pid, data['num']))
            try:
                html = _live(f'info_{pid}.html')
            except FileNotFoundError:
                return Resp('', 404)
            return Resp(html.replace('<td>0</td>', f"<td>{data['num']}</td>", 1))
        return super().post(url, data=data, **k)


def _live_crawler(site=None):
    site = site or LiveSite()
    c = crawler(site)
    c._names = {}
    return c, site


@live
def test_geo_live_empty_search_is_empty_not_error():
    c, _ = _live_crawler(LiveSite('search_empty.html'))
    assert c.search('없는품목') == []


@live
def test_geo_live_four_rows_parse_and_display():
    E = _expected()
    c, site = _live_crawler()
    valid, damaged, unidentifiable = c._search_rows('x')
    assert not damaged and not unidentifiable
    assert {p: [r['local'], r['other_total'], r['code']] for p, r in valid.items()} == E['search_rows']
    for pid, name in E['names'].items():
        assert valid[pid]['name'] == name
    rows = {r.product_id: r for r in c.search('x')}
    assert {p: [r.local_stock, r.other_stock, r.other_move_code, r.price] for p, r in rows.items()} == {
        p: v for p, v in E['display'].items() if p in rows}
    assert set(rows) == {p for p, v in E['display'].items() if v[0] or v[1] or p in E['names']}
    assert [p for p, n in site.nums] == list(E['search_rows'])        # 4행 모두 상세 요청
    assert all(n == E['search_rows'][p][0] for p, n in site.nums)      # num = 자기센터 재고(사이트와 같음)


@live
def test_geo_live_order_stocks_per_product():
    E = _expected()
    c, _ = _live_crawler()
    for pid, want in E['stocks'].items():
        assert list(c._get_product_stocks(pid, {'insurance_code': E['insurance_code']})) == want, pid


@live
def test_geo_live_history_name_match_only_same_product():
    E = _expected()
    c, _ = _live_crawler()
    c.search('x')
    R = E['roles']
    pid = R['multi_center']
    target, sibling = E['names'][pid], E['names'][R['sibling']]
    assert c._matches(pid, target)
    assert c._matches(pid, ' ' + target.replace(' ', '  ') + ' ')
    assert not c._matches(pid, sibling)
    assert not c._matches(pid, target + '(주)')


@live
def test_geo_live_history_excerpt_is_incomplete_and_storage_reads_with_synthetic_footer():
    """10/02 주문내역 발췌는 마지막 주문번호 줄이 잘려 원문 그대로는 판독 오류가 맞다.
    합성 주문번호 줄(DA9999-*)을 덧붙였음을 명시하고 창고 칸 판독만 검증한다."""
    E = _expected()['history_excerpt']
    raw = _live(E['file'])
    c = crawler(Site({}, {}))

    class Hist:
        def __init__(self, body):
            self.body = body

        def get(self, url, **k):
            return Resp(f'<input id="dtpFrom" name="dtpFrom"><table>{self.body}</table>')
    c.session = Hist(raw)
    with pytest.raises(gw.HistoryReadError):
        c._order_rows(('2026-09-02', '2026-10-02'))
    synthetic = raw + ('<tr class="tfoot"><td><span class="title">주문번호</span>DA9999-0000001</td></tr>')
    c.session = Hist(synthetic)
    rows = c._order_rows(('2026-09-02', '2026-10-02'))
    assert [r[1] for r in rows[E['first_order_no']]] == [E['storage_local']]
    assert [r[1] for r in rows['DA9999-0000001']] == [E['storage_other']]


@live
def test_geo_live_popup_center_equals_history_storage_and_other_center_order_is_accepted():
    """I-1: 실측 상세 팝업 센터명과 실측 주문내역 창고 표기가 같다 — 타센터 단계가 접수로 판정된다."""
    E = _expected()
    R = E['roles']
    popup = gw.GeoWebCrawler._centers(_bs(_live(f"info_{R['multi_center']}.html"), 'html.parser'))
    assert popup[0][0] == E['popup_center_multi_center']
    assert gw._norm(popup[0][0]) == gw._norm(E['history_excerpt']['storage_other'])
    pid = R['other_only']
    site = LiveSite(local={pid: 0}, other={pid: E['stocks'][pid][1]})
    site.names = dict(E['names'])
    site.storage_override = E['history_excerpt']['storage_other']   # 실측 주문내역 창고 표기로 기록
    c, _ = _live_crawler(site)
    r = c.order(pid, 10, insurance_code=R['other_only_insurance'])
    assert r.success and r.fulfilled_quantity == 10
    assert site.sends == [{(pid, E['stocks'][pid][2]): 10}]


@live
def test_geo_live_public_order_wrapper_three_products():
    E = _expected()
    R = E['roles']
    for pid, qty, want in [(R['multi_center'], 10, 'ok'), (R['sibling'], 10, 'stock_zero'), (R['other_only'], 10, 'ok')]:
        local, other, move = E['stocks'][pid]
        site = LiveSite(local={pid: local}, other={pid: other})
        site.names = dict(E['names'])
        site.storage_override = E['history_excerpt']['storage_other'] if not local else None
        c, _ = _live_crawler(site)
        r = c.order(pid, qty, insurance_code=E['insurance_code'] if pid != R['other_only'] else R['other_only_insurance'])
        assert r.reason_code == want, pid
        assert len(site.sends) == (0 if want == 'stock_zero' else 1), pid


# ── 타센터 확인 불가(I-2)·세션 상실·재조회(M-2) ─────────────────────────

def _detail_override(site, fn):
    original = site.post
    def post(url, **kw):
        r = original(url, **kw)
        if 'PartialProductInfo/' in url:
            return fn(r) or r
        return r
    site.post = post
    return site


def test_geo_blank_placeholder_row_in_popup_is_skipped():
    site = Site({'A': 3}, {}, centers={'A': [['센터가', 'c1', 4]]})
    _detail_override(site, lambda r: setattr(r, 'text', r.text.replace(
        '<tbody>', '<tbody><tr><td colspan="20" style="border-bottom:none;">&nbsp;</td></tr>', 1)))
    c = crawler(site)
    assert c._get_product_stocks('A') == (3, 4, 'c1')
    assert c._order_bare('A', 5).success and site.sends == [{('A', ''): 3}, {('A', 'c1'): 2}]


@pytest.mark.parametrize('break_detail', ['http404', 'http500', 'timeout', 'maintenance', 'popup_dup', 'popup_missing'])
def test_geo_detail_unavailable_keeps_local_only(break_detail):
    import requests
    site = Site({'A': 3}, {}, centers={'A': [['센터가', 'c1', 4]]})
    def fn(r):
        if break_detail == 'http404': r.status_code = 404
        if break_detail == 'http500': r.status_code = 500
        if break_detail == 'timeout': raise requests.Timeout('상세 시간 초과')
        if break_detail == 'maintenance': r.text = '<html>maintenance</html>'
        if break_detail == 'popup_dup': r.text = r.text + '<div class="another_center_pop"><table><tbody></tbody></table></div>'
        if break_detail == 'popup_missing': r.text = r.text.split('<div class="another_center_pop">')[0]
    _detail_override(site, fn)
    c = crawler(site)
    assert c._get_product_stocks('A') == (3, 0, '')
    r = c._order_bare('A', 5)
    assert site.sends == [{('A', ''): 3}] and r.success and r.adjusted_quantity == 3
    assert r.shortfall_reason == 'stopped'


@pytest.mark.parametrize('break_detail', ['http404', 'timeout', 'maintenance'])
def test_geo_detail_unavailable_with_zero_local_is_not_sent_not_stock_zero(break_detail):
    import requests
    site = Site({'A': 0}, {}, centers={'A': [['센터가', 'c1', 4]]})
    def fn(r):
        if break_detail == 'http404': r.status_code = 404
        if break_detail == 'timeout': raise requests.Timeout('상세 시간 초과')
        if break_detail == 'maintenance': r.text = '<html>maintenance</html>'
    _detail_override(site, fn)
    c = crawler(site)
    assert c.presend_stock({'product_id': 'A'}) is None
    r = c._order_bare('A', 2)
    assert r.reason_code == 'not_sent' and site.sends == []


@pytest.mark.parametrize('loss', ['redirect', '401', '403'])
def test_geo_detail_session_loss_blocks_order_and_invalidates_login(loss):
    site = Site({'A': 9}, {})
    def fn(r):
        if loss == 'redirect': r.url = LOGIN.url
        else: r.status_code = int(loss)
    _detail_override(site, fn)
    c = crawler(site)
    r = c._order_bare('A', 2)
    assert r.reason_code == 'not_sent' and site.sends == [] and c._logged_in is False


def test_geo_display_keeps_other_rows_when_one_detail_times_out():
    import requests
    site = with_search(Site({'A': 9, 'B': 0}, {'B': 4}), search_html() + search_html(pid='B', stock='0', other='4'))
    original = site.post
    def post(url, **kw):
        if url.endswith('PartialProductInfo/B'):
            raise requests.Timeout('형제 상세 시간 초과')
        return original(url, **kw)
    site.post = post
    rows = crawler(site).search('x')
    assert [r.product_id for r in rows] == ['A']           # B 는 자기센터 0·타센터 확인 불가라 표시 제외


def test_geo_mismatch_rereads_once_and_uses_second_bundle():
    site = with_search(Site({'A': 0}, {}, centers={'A': [['센터가', 'c1', 5]]}), search_html(stock='0', other='5'))
    reads = []
    def fn(r):
        reads.append(1)
        if len(reads) == 1:
            r.text = r.text.replace('<td>5</td>', '<td>4</td>')   # 첫 상세는 4 (검색 5) → 불일치
    _detail_override(site, fn)
    r = crawler(site)._order_bare('A', 5)
    assert len(reads) == 2 and r.success and site.sends == [{('A', 'c1'): 5}]


def test_geo_mismatch_twice_uses_local_only_and_zero_local_not_sent():
    site = with_search(Site({'A': 2}, {}, centers={'A': [['센터가', 'c1', 4]]}), search_html(stock='2', other='5'))
    r = crawler(site)._order_bare('A', 5)
    assert site.sends == [{('A', ''): 2}] and r.shortfall_reason == 'stopped'
    site = with_search(Site({'A': 0}, {}, centers={'A': [['센터가', 'c1', 4]]}), search_html(stock='0', other='5'))
    c = crawler(site)
    assert c.presend_stock({'product_id': 'A'}) is None
    assert c._order_bare('A', 2).reason_code == 'not_sent' and site.sends == []


def test_geo_mismatch_reread_damaged_target_does_not_fall_back_to_first_read():
    site = Site({'A': 0}, {}, centers={'A': [['센터가', 'c1', 5]]})
    searches = []
    original = site.post
    def post(url, **kw):
        if url.endswith('PartialSearchProduct'):
            searches.append(1)
            return Resp(search_html(stock='0', other='9') if len(searches) == 1 else search_html(stock='bad'))
        return original(url, **kw)
    site.post = post
    c = crawler(site)
    assert c._order_bare('A', 2).reason_code == 'not_sent' and site.sends == [] and len(searches) == 2


# ── 센터 목록 모호성(S-1)·다단계(S-5) ──────────────────────────────────

def test_geo_duplicate_center_names_make_other_centers_unavailable():
    site = Site({'A': 1}, {}, centers={'A': [['센터가', 'c1', 1], ['센터 가', 'c2', 5]]})
    c = crawler(site)
    assert c._get_product_stocks('A') == (1, 0, '')
    r = c._order_bare('A', 3)
    assert site.sends == [{('A', ''): 1}] and r.shortfall_reason == 'stopped'


def test_geo_third_stage_guard_failure_keeps_two_stages_and_restores_cart():
    site = Site({'A': 1}, {}, cart={('Z', ''): 3}, centers={'A': [['C1', 'c1', 2], ['C2', 'c2', 5]]})
    c = crawler(site)
    calls = []
    def guard():
        calls.append(1)
        if len(calls) == 5:                          # 단계마다 guard 2회 — 3단계 첫 guard 에서 실패
            raise RuntimeError('소유권 상실')
    c.send_guard = guard
    r = c._order_bare('A', 6)
    assert site.sends == [{('A', ''): 1}, {('A', 'c1'): 2}]
    assert r.success and r.fulfilled_quantity == 3 and r.shortfall_reason == 'stopped'
    assert site.cart == {('Z', ''): 3}


def test_geo_storage_mismatch_stops_following_batch_item():
    site = Site({'A': 0, 'B': 9}, {}, centers={'A': [['C1', 'c1', 5]]}, storage_override='엉뚱한센터')
    res = crawler(site).order_batch([{'product_id': 'A', 'quantity': 2}, {'product_id': 'B', 'quantity': 1}])
    assert [r.reason_code for r in res] == ['send_unknown', 'not_sent'] and len(site.sends) == 1


# ── 상위 문구(M-1): 재고 부족이 아닌 미전송 ─────────────────────────────

def test_stopped_shortfall_is_not_described_as_stock_shortage():
    from domae_mcp.cloud.scheduler import _quick_order_message
    from domae_mcp.cloud.fallback import cart_action_after_order
    stopped = gw.GeoWebCrawler._result(5, 3, 'stock_adjusted', 9, stopped=True)
    stock = gw.GeoWebCrawler._result(5, 3, 'stock_adjusted', 3)
    assert '재고 부족' not in _quick_order_message('지오영', 'X', 5, stopped, True)
    assert '재고 부족' in _quick_order_message('지오영', 'X', 5, stock, True)
    assert '재고 부족' not in cart_action_after_order({'quantity': 5}, stopped)[2]
    assert '재고 부족' in cart_action_after_order({'quantity': 5}, stock)[2]
