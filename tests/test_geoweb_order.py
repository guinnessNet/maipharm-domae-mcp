# tests/test_geoweb_order.py
"""지오영 — session.get/post 만 대역. 장바구니·주문내역 HTML 은 실측 구조(2026-10-02)를 따른다."""
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
NAMES = {"A": ("가상정M", "30T", "바에스티"), "B": ("가상정U", "30T", "라제약"), "Z": ("가상정V", "30T", "마바이오")}


class Site:
    def __init__(self, local, other, cart=None, logged_in=True, delete_works=True, accept=True,
                 lose_reply=False, history_lag=0, corrupt_cart=False):
        self.local, self.other = dict(local), dict(other)
        self.cart = dict(cart or {})                         # {(pid, move): qty}
        self.logged_in, self.delete_works, self.accept = logged_in, delete_works, accept
        self.lose_reply, self.history_lag, self.corrupt_cart = lose_reply, history_lag, corrupt_cart
        self.orders, self.sends, self.reads_since_send = [], [], None

    def inject_foreign(self, pid, qty):
        n, u, m = NAMES[pid]
        self.orders.append((f"DA2610-9{len(self.orders):06d}", [(f"{n} {u} {m}", "자기센터나(가상)", qty)]))

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
                    n, u, mk = NAMES[p]
                    rows.append((f"{n} {u} {mk}", "자기센터나(가상)" if not m else "타센터나(가상)", q))
                    (self.local if not m else self.other)[p] -= q
                self.orders.append((f"DA2610-{len(self.orders):07d}", rows))
                self.cart = {}
            if self.lose_reply:
                raise TimeoutError("응답 유실")
            return Resp("ok")
        if "PartialProductInfo" in url:
            p = url.rsplit("/", 1)[1]
            other = self.other.get(p, 0)
            pop = (f'<div class="another_center_pop"><table><tbody><tr><td>c</td><td>{other}</td>'
                   f'<td><input data-code="MV"></td></tr></tbody></table></div>') if other else ""
            return Resp(f'<table><tbody><tr></tr><tr></tr><tr><td>1000</td></tr><tr><td>{self.local.get(p, 0)}</td></tr>'
                        f'</tbody></table>{pop}')
        raise AssertionError(url)


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
    assert crawler(site).order("A", 2, product_name="가상정M").success and len(site.sends) == 1


def test_geo_history_lag_is_unknown_not_resent():
    site = Site({"A": 9}, {}, history_lag=99)
    r = crawler(site).order("A", 2, product_name="가상정M")
    assert r.reason_code == "send_unknown" and len(site.sends) == 1


def test_geo_not_accepted_is_unknown_not_rejected():
    site = Site({"A": 9}, {}, accept=False)
    r = crawler(site).order("A", 2, product_name="가상정M")
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
    assert crawler(site).order("A", 2, product_name="가상정M").reason_code == "send_unknown"


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
            site.orders.append(("DA2610-8000001", [("가상정M플러스 130T 바에스티", "자기센터나(가상)", 2)]))
        return res
    site.post = post
    assert crawler(site).order("A", 2, product_name="가상정M").reason_code == "send_unknown"


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


def search_html(pid="A", code="123456789", stock="9", name=None, unit="30T", maker="바에스티"):
    return ('<table><tr><td>x</td><td>'+code+'</td><td>'+maker+'</td><td>'+(NAMES[pid][0] if name is None else name)+
            '</td><td>'+unit+'</td><td>'+stock+'</td><td><li>'+pid+'</li></td></tr></table>')


def with_search(site, html=None):
    original = site.post
    def post(url, **kw):
        if url.endswith('PartialSearchProduct'):
            return Resp(html if html is not None else search_html())
        return original(url, **kw)
    site.post = post
    return site


@pytest.mark.parametrize('stock', ['garbage', '-1', '1,2', '1.5', '', '1 000'])
def test_geo_invalid_detail_stock_is_unavailable(stock):
    site = Site({'A': 9}, {})
    original = site.post
    def post(url, **kw):
        r = original(url, **kw)
        if 'PartialProductInfo/' in url:
            r.text = r.text.replace('<td>9</td>', '<td>'+stock+'</td>')
        return r
    site.post = post
    c = crawler(site)
    assert c._get_product_stocks('A') == (None, 0, '')
    assert c._order_bare('A', 2).reason_code == 'not_sent'
    assert site.sends == []


def test_geo_search_sets_exact_metadata_and_presend_insurance():
    site = with_search(Site({'A': 9}, {'A': 4}), search_html(stock='1,234'))
    c = crawler(site)
    result = c.search('A')[0]
    assert result.local_stock == 1234 and result.other_stock == 4
    assert c._names['A'] == NAMES['A']
    assert c.presend_stock({'product_id': 'A', 'insurance_code': '000000000'}) == 13
    assert c.presend_stock({'product_id': 'A', 'insurance_code': '123456789'}) == 13


@pytest.mark.parametrize('metadata', [('가상정M', '', '바에스티'), ('', '30T', '바에스티')])
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
        if change == 'empty_name': r.text = r.text.replace('가상정M 30T 바에스티','')
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


@pytest.mark.parametrize('new_rows', [[('가상정M 30T 바에스티','자기센터나(가상)',2), ('가상정U 30T 라제약','자기센터나(가상)',1)], [('가상정M 30T 다른제약','자기센터나(가상)',2)]])
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


def test_geo_multi_other_rows_keep_established_sum_and_raw_first_code():
    site = Site({'A':3}, {})
    original = site.post
    def post(url, **kw):
        r = original(url, **kw)
        if 'PartialProductInfo/' in url:
            r.text += '<div class="another_center_pop"><table><tbody><tr><td>x</td><td>1,000</td><td><input data-code="raw-first"></td></tr><tr><td>y</td><td>5</td><td><input data-code="raw-second"></td></tr></tbody></table></div>'
        return r
    site.post = post
    assert crawler(site)._get_product_stocks('A') == (3,1005,'raw-first')


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
    assert crawler(site)._get_product_stocks('A') == (None,0,'')

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
            site.orders[-1] = (no,[('가상정M\t30T\n바에스티', rows[0][1],rows[0][2])])
        return r
    site.post = post
    assert crawler(site)._order_bare('A',2).success


def test_geo_caller_description_does_not_replace_search_identity():
    for use_wrapper in (False,True):
        site = with_search(Site({'A':9}, {}),search_html(code=''))
        c = crawler(site)
        method = c.order if use_wrapper else c._order_bare
        r = method('A',2, insurance_code='123456789', product_name='가상정M 30T (바에스티)')
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
    assert c.presend_stock({'product_id':'A', 'insurance_code':'123456789', 'product_name':'가상정M 30T'}) == 9
    assert keywords == ['123456789','가상정M30T']
    keywords.clear()
    assert c.presend_stock({'product_id':'A','insurance_code':'１２３４５６７８９','product_name':'가상정M 30T'}) == 9
    assert keywords == ['가상정M30T','가상정M 30T']


@pytest.mark.parametrize('html', [search_html(stock='1,2'), search_html().replace('<td><li>A</li></td>','<td></td>'), search_html().replace('<td>9</td>','')])
def test_geo_search_malformed_rows_not_stock_zero(html):
    site = with_search(Site({'A':9}, {}),html)
    c = crawler(site)
    with pytest.raises(ValueError): c.search('A')
    assert c.presend_stock({'product_id':'A'}) is None


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
            supported = '123456789' if with_insurance else '가상정M30T'
            return Resp(search_html() if keyword == supported else '<table><tr><td>검색된 제품이 없습니다.</td></tr></table>')
        return original(url, **kw)
    site.post = post
    c = crawler(site)
    c._names = {}
    item = {'product_id':'A','quantity':2,'product_name':'가상정M 30T'}
    if with_insurance:
        item['insurance_code'] = '123456789'
    if entrypoint == 'order_batch':
        r = c.order_batch([item])[0]
    else:
        r = getattr(c,entrypoint)('A',2,**{k:v for k,v in item.items() if k not in ('product_id','quantity')})
    assert r.success and r.fulfilled_quantity == 2
    assert queries == ['123456789' if with_insurance else '가상정M30T']
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
    '<tr><td colspan="7">검색된 제품이 없습니다.</td></tr>',
    '<tr><td>검색된 제품이 없습니다.</td><td>bad</td></tr>',
    '<tr><td>잠시 후 다시 시도</td></tr>',
    search_html().replace('<td>x</td>', '<td>검색된 제품이 없습니다.</td>').replace('<td>9</td>', '<td>bad</td>'),
])
def test_geo_notice_text_or_short_cells_do_not_exempt_damaged_real_row(damaged):
    c = crawler(with_search(Site({'A': 9}, {}), damaged))
    with pytest.raises(ValueError):
        c.search('A')


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
