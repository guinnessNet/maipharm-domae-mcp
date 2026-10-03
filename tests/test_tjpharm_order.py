# tests/test_tjpharm_order.py
"""티제이팜 — session.post 만 대역. 판독·대조·주문내역 판정·복원은 실제 코드."""
import importlib.util
import json
import os
import sys
sys.path.insert(0, "src")

import fakeredis

from domae_mcp.core.crawlers.cart_snapshot import CartSnapshot

PATH = os.environ.get("TJPHARM_PY", os.path.expanduser(
    "~/.config/superpowers/worktrees/pharmsquare-server-main/order-resilience/prisma/seeds/domae-crawlers/tjpharm.py"))
spec = importlib.util.spec_from_file_location("tjpharm_under_test", PATH)
tj = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tj)
from datetime import datetime, timedelta, timezone
import pytest
TODAY = datetime.now(timezone(timedelta(hours=9))).strftime("%Y%m%d")


class Resp:
    def __init__(self, text, status=200):
        self.text, self.status_code = text, status


class Site:
    """catalog: {code: 재고} — 검색·토큰 제공 대상(복원 대상 Z 포함). basket: {code: qty}."""
    def __init__(self, catalog, basket=None, logged_in=True, clear_works=True, accept=True,
                 reply="201", lose_reply=False, history_lag=0, corrupt_basket=False, add_fails=(),
                 history_fails_after_send=False):
        self.history_fails_after_send = history_fails_after_send
        self.catalog, self.basket = dict(catalog), dict(basket or {})
        self.logged_in, self.clear_works, self.accept = logged_in, clear_works, accept
        self.reply, self.lose_reply, self.history_lag = reply, lose_reply, history_lag
        self.corrupt_basket, self.add_fails = corrupt_basket, set(add_fails)
        self.orders, self.sends = [], []
        self.reads_since_send = None

    def inject_foreign(self, items):
        self.orders.append((TODAY, str(len(self.orders) + 1), dict(items)))

    def post(self, url, data=None, headers=None, **k):
        data = data or {}
        if url.endswith("/Order/basket_api.php"):
            if not self.logged_in:
                return Resp("<script> alert('로그인해주세요.'); </script>")
            rows = [{"ItemCode": c, "OQty": q, "OCst": 1000, "ItemName": c} for c, q in self.basket.items()]
            if self.corrupt_basket and rows:
                rows[0].pop("ItemCode")
            return Resp("\r\n" + json.dumps(rows))
        if url.endswith("/Order/basket_del_api.php"):
            if self.clear_works:
                if data.get("itemCode"):
                    self.basket.pop(data["itemCode"], None)
                else:
                    self.basket = {}
            return Resp(json.dumps({"StatusCode": "200"}))
        if url.endswith("/Order/basket_post_api.php"):
            if data["ItemCode"] in self.add_fails or data.get("ItemToken") != "t" + data["ItemCode"]:
                return Resp(json.dumps({"StatusCode": "400"}))
            self.basket[data["ItemCode"]] = self.basket.get(data["ItemCode"], 0) + int(data["Qty"])
            return Resp(json.dumps({"StatusCode": "201"}))
        if url.endswith("/Order/item_api.php"):
            kw = data.get("name") if data.get("name") in self.catalog else None   # 실제 요청 필드 name
            rows = [{"ItemCode": kw, "InvQty": self.catalog[kw], "Cst": 1000, "ItemToken": "t" + kw,
                     "ItemName": kw, "HiCode": ""}] if kw else []
            return Resp(json.dumps({"ResultSet": rows}))
        if url.endswith("/Order/basket_send_api.php"):
            self.sends.append(dict(self.basket))
            self.reads_since_send = 0
            if self.accept:
                self.orders.append((TODAY, str(len(self.orders) + 1), dict(self.basket)))
                for c, q in self.basket.items():
                    self.catalog[c] -= q
                self.basket = {}
            if self.lose_reply:
                raise TimeoutError("응답 유실")
            return Resp(json.dumps({"StatusCode": self.reply if self.accept else "400", "Message": "m"}))
        if url.endswith("/OrderList/order_list_ajax.php"):
            if self.history_fails_after_send and self.reads_since_send is not None:
                return Resp("<html>error</html>", 500)
            visible = self.orders
            if self.reads_since_send is not None:
                self.reads_since_send += 1
                if self.reads_since_send <= self.history_lag and self.accept:
                    visible = self.orders[:-1]
            return Resp(json.dumps([{"OrdDate": d, "OrdNo": n, "Items": [{"ItemCode": c, "OrdQty": q}
                                                                        for c, q in it.items()]}
                                    for d, n, it in visible if data["sdate"] <= d <= data["edate"]]))
        raise AssertionError(url)


def crawler(site):
    c = tj.TjPharmCrawler.__new__(tj.TjPharmCrawler)
    c.session = site
    c._item_tokens, c._item_prices, c._basket_names = {}, {}, {}
    c._login_id = c._login_pw = ""
    c._logged_in = True
    c.ensure_login = lambda *a: True
    c.login = lambda *a: site.logged_in
    c.cart_snapshot = CartSnapshot(fakeredis.FakeRedis(), "m", "티제이팜")
    c._history_wait = 0
    return c

def leftover(c, snap):
    """이전 실행이 남긴 기록(잠금은 이미 풀림)."""
    old = CartSnapshot(c.cart_snapshot._r, "m", c.cart_snapshot._s)
    old.lock()
    old.save(snap)
    old.unlock()



def test_tj_untrusted_basket_keeps_cart_and_sends_nothing():
    site = Site({"A": 9, "Z": 9}, basket={"Z": 3}, logged_in=False)
    assert crawler(site)._order_bare("A", 2).reason_code == "not_sent" and site.sends == [] and site.basket == {"Z": 3}


def test_tj_corrupt_row_sends_nothing():
    site = Site({"A": 9, "Z": 9}, basket={"Z": 3}, corrupt_basket=True)
    assert crawler(site)._order_bare("A", 2).reason_code == "not_sent" and site.sends == []


def test_tj_clear_failure_sends_nothing():
    site = Site({"A": 9, "Z": 9}, basket={"Z": 3}, clear_works=False)
    assert crawler(site)._order_bare("A", 2).reason_code == "not_sent" and site.sends == []


def test_tj_unrestorable_cart_is_not_touched():
    site = Site({"A": 9}, basket={"GONE": 3})          # GONE 은 검색되지 않아 토큰 없음
    assert crawler(site)._order_bare("A", 2).reason_code == "not_sent" and site.basket == {"GONE": 3}


def test_tj_success_with_history_and_restore():
    site = Site({"A": 9, "Z": 9}, basket={"Z": 3})
    c = crawler(site)
    r = c._order_bare("A", 2)
    assert r.success and site.sends == [{"A": 2}] and site.basket == {"Z": 3} and c.cart_snapshot.load() is None


def test_tj_lost_reply_but_history_shows_accepted():
    site = Site({"A": 9}, lose_reply=True)
    r = crawler(site).order("A", 2, product_name="A")
    assert r.success and len(site.sends) == 1


def test_tj_lost_reply_and_history_lag_is_unknown_not_resent():
    site = Site({"A": 9}, lose_reply=True, history_lag=99)
    r = crawler(site).order("A", 2, product_name="A")
    assert r.reason_code == "send_unknown" and len(site.sends) == 1


def test_tj_non201_without_history_is_unknown_not_rejected():
    site = Site({"A": 9}, accept=False)
    r = crawler(site).order("A", 2, product_name="A")
    assert r.reason_code == "send_unknown" and len(site.sends) == 1


def test_tj_foreign_order_is_not_counted():
    site = Site({"A": 9}, accept=False)
    orig = site.post

    def post(url, **kw):
        res = orig(url, **kw)
        if url.endswith("basket_send_api.php"):
            site.inject_foreign({"A": 1})                 # 다른 세션이 같은 품목 1개 주문
        return res
    site.post = post
    assert crawler(site).order("A", 2, product_name="A").reason_code == "send_unknown"


def test_tj_presend_stock_adjust():
    site = Site({"A": 2, "B": 0})
    res = crawler(site).order_batch([{"product_id": "A", "quantity": 5}, {"product_id": "B", "quantity": 1}])
    assert site.sends == [{"A": 2}]
    assert res[0].success and res[0].adjusted_quantity == 2 and res[1].reason_code == "stock_zero"


def test_tj_batch_unknown_marks_all_sent_items():
    site = Site({"A": 9, "B": 9}, lose_reply=True, history_lag=99)
    res = crawler(site).order_batch([{"product_id": "A", "quantity": 1}, {"product_id": "B", "quantity": 1}])
    assert len(site.sends) == 1 and {r.reason_code for r in res} == {"send_unknown"}


def test_tj_201_with_history_read_failure_is_unknown():
    site = Site({"A": 9}, history_fails_after_send=True)
    assert crawler(site).order("A", 2, product_name="A").reason_code == "send_unknown"


def test_tj_two_new_orders_is_unknown():
    site = Site({"A": 9})
    orig = site.post

    def post(url, **kw):
        res = orig(url, **kw)
        if url.endswith("basket_send_api.php"):
            site.inject_foreign({"A": 2})                 # 약사도 같은 주문 → 새 주문 2건
        return res
    site.post = post
    assert crawler(site).order("A", 2, product_name="A").reason_code == "send_unknown"


def test_tj_pending_snapshot_blocks_and_keeps_cart():
    site = Site({"A": 9, "Z": 9}, basket={"Y": 1})
    c = crawler(site)
    leftover(c, {"Z": 3})                                 # 이전 실행이 Z 를 되돌리지 못하고 끝남
    r = c._order_bare("A", 2)                             # 지금 장바구니 Y 는 이전 기록과 다름 → 막음
    assert r.reason_code == "not_sent" and r.no_retry and site.sends == [] and site.basket == {"Y": 1}


def test_tj_user_item_added_during_run_is_not_touched():
    site = Site({"A": 9, "Z": 9, "X": 9}, basket={"Z": 3})
    orig = site.post

    def post(url, **kw):
        res = orig(url, **kw)
        if url.endswith("basket_send_api.php"):
            site.basket["X"] = 1                          # 약사가 전송 직후 X 를 담음
        return res
    site.post = post
    c = crawler(site)
    c._order_bare("A", 2)
    assert site.basket == {"X": 1}                        # 정리·복원 안 함(약사 품목 보존)
    assert c.cart_snapshot._r.get(c.cart_snapshot.failed_key)


def test_tj_empty_snapshot_restore_read_failure_is_recorded():
    site = Site({"A": 9})
    c = crawler(site)
    orig = site.post

    def post(url, **kw):
        res = orig(url, **kw)
        if url.endswith("basket_send_api.php"):
            site.logged_in = False                        # 전송 후 판독 불가
        return res
    site.post = post
    c._order_bare("A", 2)
    assert c.cart_snapshot._r.get(c.cart_snapshot.failed_key)


def test_tj_send_guard_blocks_send():
    site = Site({"A": 9})
    c = crawler(site)

    def guard():
        raise RuntimeError("소유권 상실")
    c.send_guard = guard
    assert c._order_bare("A", 2).reason_code == "not_sent" and site.sends == []


def test_tj_add_failure_detected_by_match():
    site = Site({"A": 9, "B": 9}, add_fails={"B"})
    res = crawler(site).order_batch([{"product_id": "A", "quantity": 1}, {"product_id": "B", "quantity": 1}])
    assert site.sends == [] and {r.reason_code for r in res} == {"not_sent"}

@pytest.mark.parametrize('qty', [True, False, 1.5, '1.5', 0, -1, None, {}, []])
def test_tj_corrupt_basket_quantity(qty):
    site = Site({'A': 9}, basket={'Z': qty})
    r = crawler(site)._order_bare('A', 1)
    assert r.reason_code == 'not_sent' and not site.sends and site.basket == {'Z': qty}

@pytest.mark.parametrize('qty', [True, 1.5, '1.5', 0, -1, None])
def test_tj_invalid_request_is_not_truncated(qty):
    site = Site({'A': 9}, basket={'Z': 2})
    r = crawler(site).order('A', qty, product_name='A')
    assert r.reason_code == 'not_sent' and r.no_retry and not site.sends and site.basket == {'Z': 2}

@pytest.mark.parametrize('qty', [True, 1.5, '1.5', -1, None])
def test_tj_invalid_stock_is_unknown_before_touch(qty):
    site = Site({'A': qty, 'Z': 9}, basket={'Z': 2})
    r = crawler(site).order('A', 2, product_name='A')
    assert r.reason_code == 'not_sent' and r.no_retry and not site.sends and site.basket == {'Z': 2}

@pytest.mark.parametrize('row', [None, {}, {'OrdDate': TODAY, 'OrdNo': '1', 'Items': None},
    {'OrdDate': TODAY, 'OrdNo': '1', 'Items': [None]},
    {'OrdDate': TODAY, 'OrdNo': '1', 'Items': [{'ItemCode': 'A', 'OrdQty': True}]},
    {'OrdDate': TODAY, 'OrdNo': '1', 'Items': [{'ItemCode': '', 'OrdQty': 1}]},
    {'OrdDate': TODAY, 'OrdNo': '1', 'Items': [{'ItemCode': 'A', 'OrdQty': 1.5}]}])
def test_tj_corrupt_history_prevents_send(row):
    site = Site({'A': 9}); orig = site.post
    def post(url, **kw):
        return Resp(json.dumps([row])) if url.endswith('order_list_ajax.php') else orig(url, **kw)
    site.post = post
    assert crawler(site)._order_bare('A', 1).reason_code == 'not_sent' and not site.sends


def test_tj_duplicate_history_key_is_not_silently_overwritten():
    site = Site({'A': 9}); orig = site.post
    def post(url, **kw):
        if url.endswith('order_list_ajax.php') and site.sends:
            return Resp(json.dumps([{'OrdDate': TODAY, 'OrdNo': '1', 'Items': [{'ItemCode': 'A', 'OrdQty': q}]} for q in [9, 2]]))
        return orig(url, **kw)
    site.post = post
    assert crawler(site).order('A', 2).reason_code == 'send_unknown' and len(site.sends) == 1


def test_tj_201_with_successful_empty_history_is_accepted():
    site = Site({'A': 9}, history_lag=99)
    r = crawler(site).order('A', 2)
    assert r.success and len(site.sends) == 1 and site.reads_since_send == 4


def test_tj_guard_cart_race_preserves_foreign_items():
    site = Site({'A': 9, 'Z': 9}, basket={'Z': 2}); c = crawler(site)
    c.send_guard = lambda: site.basket.update({'X': 1})
    r = c._order_bare('A', 2)
    assert r.reason_code == 'not_sent' and not site.sends and site.basket == {'A': 2, 'X': 1}
    assert c.cart_snapshot._r.get(c.cart_snapshot.failed_key)


def test_tj_fixed_kst_dates_request_and_body(monkeypatch):
    site = Site({'A': 9}); orig = site.post; calls=[]
    def post(url, **kw):
        calls.append((url, kw)); return orig(url, **kw)
    site.post=post
    # Midnight can pass while polling, but the date range must never move.
    ticks=iter([datetime(2026, 10, 3, 23, 59, tzinfo=timezone(timedelta(hours=9))),
                datetime(2026, 10, 4, 0, 1, tzinfo=timezone(timedelta(hours=9)))])
    class Clock:
        @classmethod
        def now(cls, tz):
            return next(ticks)
    monkeypatch.setattr(tj, 'datetime', Clock)
    assert crawler(site).order('A', 2).success
    history=[kw for url,kw in calls if url.endswith('order_list_ajax.php')]
    assert len(history) >= 2 and all(kw['data']=={'sdate':'20261002','edate':'20261003'} for kw in history)
    send=[kw for url,kw in calls if url.endswith('basket_send_api.php')]
    assert send==[{'data': {'ip':'','memo':''}, 'headers':tj.TjPharmCrawler._ORDER_HEADERS}]


def test_tj_metadata_reaches_insurance_first_stock_search():
    site=Site({'A':9}); orig=site.post; queries=[]
    def post(url, **kw):
        if url.endswith('item_api.php'):
            queries.append(kw['data']['name'])
        return orig(url, **kw)
    site.post=post
    assert crawler(site).order('A',2,product_name='A',insurance_code='123456789',unit='box',maker='maker').success
    assert queries[0]=='123456789'


def test_tj_duplicate_pid_reserves_one_stock_budget():
    site=Site({'A':3})
    r=crawler(site).order_batch([{'product_id':'A','quantity':2}, {'product_id':'A','quantity':2}])
    assert site.sends==[{'A':3}] and r[0].success and r[1].adjusted_quantity==1


def test_tj_reduced_unknown_never_claims_fulfillment():
    site=Site({'A':2},lose_reply=True,history_lag=99)
    r=crawler(site).order('A',5)
    assert r.reason_code=='send_unknown' and r.original_quantity==5 and r.adjusted_quantity is None and r.fulfilled_quantity==0 and r.available_stock==2 and len(site.sends)==1


def test_tj_metadata_forwarding_to_bare_and_send_cart():
    c=crawler(Site({'A':9})); seen=[]
    c._send_cart=lambda items: seen.extend(items) or [tj.OrderResult(success=True)]
    c.order('A',2,insurance_code='123456789',unit='box',maker='maker',product_name='A',custom='value')
    assert seen==[{'product_id':'A','quantity':2,'insurance_code':'123456789','unit':'box','maker':'maker','product_name':'A','custom':'value'}]


def test_tj_restore_token_search_failure_does_not_touch():
    site=Site({'A':9,'Z':9},basket={'Z':2}); orig=site.post
    def post(url, **kw):
        if url.endswith('item_api.php'): raise TimeoutError('catalog unavailable')
        return orig(url, **kw)
    site.post=post
    r=crawler(site)._order_bare('A',2)
    assert r.no_retry and r.reason_code=='not_sent' and site.basket=={'Z':2} and not site.sends

@pytest.mark.parametrize('visible,accepted', [(False,False),(True,True)])
def test_tj_http_error_201_requires_history_evidence(visible, accepted):
    site=Site({'A':9},history_lag=0 if visible else 99); orig=site.post
    def post(url, **kw):
        result=orig(url, **kw)
        if url.endswith('basket_send_api.php'): result.status_code=500
        return result
    site.post=post
    r=crawler(site).order('A',2)
    assert r.success==accepted and len(site.sends)==1
    if not accepted: assert r.reason_code=='send_unknown'


def test_tj_invalid_batch_item_is_excluded_individually():
    site=Site({'A':9,'B':9})
    r=crawler(site).order_batch([{'product_id':'A','quantity':1.5},{'product_id':'B','quantity':2}])
    assert r[0].reason_code=='not_sent' and r[0].no_retry and r[1].success and site.sends==[{'B':2}]


def test_tj_restore_uses_measured_name_then_compact_and_insurance_price():
    site=Site({'A':9,'Z':9},basket={'Z':2}); orig=site.post; queries=[]; restored=[]
    def post(url, **kw):
        if url.endswith('basket_api.php'):
            resp=orig(url, **kw); rows=json.loads(resp.text)
            for row in rows:
                if row['ItemCode']=='Z': row['ItemName']='Name With Spaces'
            return Resp(json.dumps(rows))
        if url.endswith('item_api.php'):
            query=kw['data']['name']; queries.append(query)
            if query=='NameWithSpaces':
                return Resp(json.dumps({'ResultSet':[{'ItemCode':'Z','InvQty':9,'Cst':0,'HiCst':1234,'ItemToken':'tZ'}]}))
            if query=='Z': raise AssertionError('name and compact lookup must precede code')
        if url.endswith('basket_post_api.php') and kw['data']['ItemCode']=='Z': restored.append(kw['data'])
        return orig(url, **kw)
    site.post=post
    assert crawler(site)._order_bare('A',2).success and site.basket=={'Z':2}
    assert queries[:2]==['Name With Spaces','NameWithSpaces'] and restored[0]['Cst']=='1234'


@pytest.mark.parametrize('body', [None, [], {'ResultSet':None}, {'ResultSet':[None]}, {'ResultSet':[{'ItemCode':'Z','ItemToken':False,'Cst':1000}]}])
def test_tj_malformed_restore_catalog_never_touches(body):
    site=Site({'A':9,'Z':9},basket={'Z':2}); orig=site.post
    def post(url, **kw):
        return Resp(json.dumps(body)) if url.endswith('item_api.php') else orig(url, **kw)
    site.post=post
    r=crawler(site)._order_bare('A',2)
    assert r.reason_code=='not_sent' and r.no_retry and site.basket=={'Z':2} and not site.sends


def test_tj_cart_changed_before_first_mutation_just_stops():
    site=Site({'A':9,'Z':9,'X':9},basket={'Z':2}); orig=site.post; mutations=[]
    def post(url, **kw):
        res=orig(url, **kw)
        if url.endswith('item_api.php') and kw['data']['name']=='A': site.basket['X']=1
        if url.endswith(('basket_post_api.php','basket_del_api.php')): mutations.append(url)
        return res
    site.post=post
    c=crawler(site); r=c._order_bare('A',2)
    assert r.reason_code=='not_sent' and not mutations and not site.sends and site.basket=={'Z':2,'X':1}
    assert c.cart_snapshot.load() is None


def test_tj_history_duplicate_item_rows_aggregate_exactly():
    site=Site({'A':9}); orig=site.post
    def post(url, **kw):
        if url.endswith('order_list_ajax.php') and site.sends:
            return Resp(json.dumps([{'OrdDate':TODAY,'OrdNo':'1','Items':[{'ItemCode':'A','OrdQty':'1'},{'ItemCode':'A','OrdQty':1}]}]))
        return orig(url, **kw)
    site.post=post
    assert crawler(site).order('A',2).success and len(site.sends)==1


def test_tj_http_201_with_readable_empty_history_uses_screen_contract():
    site=Site({'A':9},history_lag=99); orig=site.post
    def post(url, **kw):
        result=orig(url, **kw)
        if url.endswith('basket_send_api.php'): result.status_code=201
        return result
    site.post=post
    assert crawler(site).order('A',2).success and len(site.sends)==1


def test_tj_corruption_diagnostic_names_field_without_raw_token():
    site=Site({'A':9}); orig=site.post
    def post(url, **kw):
        if url.endswith('basket_api.php'):
            return Resp(json.dumps([{'ItemCode':'A','OQty':True,'ItemToken':'secret-token'}]))
        return orig(url, **kw)
    site.post=post
    with pytest.raises(tj.BasketReadError, match='행 0 OQty') as error:
        crawler(site)._read_basket()
    assert 'secret-token' not in str(error.value)


def test_tj_history_diagnostic_names_field_without_raw_token():
    site=Site({'A':9}); orig=site.post
    def post(url, **kw):
        if url.endswith('order_list_ajax.php'):
            return Resp(json.dumps([{'OrdDate':TODAY,'OrdNo':'1','Items':[{'ItemCode':'A','OrdQty':1.5,'ItemToken':'secret-token'}]}]))
        return orig(url, **kw)
    site.post=post
    with pytest.raises(tj.HistoryReadError, match='행 0 품목 0 OrdQty') as error:
        crawler(site)._order_keys((TODAY,TODAY))
    assert 'secret-token' not in str(error.value)


@pytest.mark.parametrize('identity', ['2026-10-03', '20261003'])
def test_tj_history_date_identity_accepts_public_order_with_fixed_request_range(identity):
    site = Site({'A': 9})
    site.orders.append((TODAY, 'existing', {'A': 1}))
    original = site.post
    requests = []
    def post(url, **kwargs):
        if url.endswith('order_list_ajax.php'):
            requests.append(kwargs['data'])
            return Resp(json.dumps([
                {'OrdDate': identity, 'OrdNo': number,
                 'Items': [{'ItemCode': code, 'OrdQty': qty} for code, qty in items.items()]}
                for _, number, items in site.orders
            ]))
        return original(url, **kwargs)
    site.post = post
    result = crawler(site).order('A', 2)
    assert result.success and result.fulfilled_quantity == 2
    assert result.message == f'주문번호 {identity}-2'
    assert site.sends == [{'A': 2}]
    yesterday = (datetime.now(timezone(timedelta(hours=9))) - timedelta(days=1)).strftime('%Y%m%d')
    assert len(requests) == 2
    assert all(request == {'sdate': yesterday, 'edate': TODAY} for request in requests)


@pytest.mark.parametrize('identity', [None, True, False, [], {}, '', '   ', 20261003])
def test_tj_history_rejects_malformed_date_identity(identity):
    site = Site({'A': 9})
    original = site.post
    def post(url, **kwargs):
        if url.endswith('order_list_ajax.php'):
            return Resp(json.dumps([{'OrdDate': identity, 'OrdNo': '1',
                                    'Items': [{'ItemCode': 'A', 'OrdQty': 2}]}]))
        return original(url, **kwargs)
    site.post = post
    result = crawler(site).order('A', 2)
    assert result.reason_code == 'not_sent' and result.no_retry
    assert site.sends == []


def metadata_site(row, basket=None):
    """실제 POST 경계에서 카탈로그만 교체하고 모든 장바구니 조작을 기록한다."""
    site = Site({'A': 9, 'Z': 9}, basket=basket)
    original = site.post
    mutations = []
    def post(url, **kwargs):
        if url.endswith(('basket_post_api.php', 'basket_del_api.php')):
            mutations.append(kwargs['data'])
        if url.endswith('item_api.php'):
            code = kwargs['data']['name']
            if code == row.get('ItemCode'):
                return Resp(json.dumps({'ResultSet': [row]}))
        return original(url, **kwargs)
    site.post = post
    return site, mutations


@pytest.mark.parametrize('prices', [{}, {'Cst': True}, {'Cst': 12.75}, {'Cst': '12.75'},
                                    {'Cst': None}, {'Cst': ''}, {'HiCst': False}])
@pytest.mark.parametrize('public', [True, False])
def test_tj_unavailable_original_price_never_touches_cart(prices, public):
    site, mutations = metadata_site({'ItemCode': 'Z', 'InvQty': 9, 'ItemToken': 'tZ', **prices}, {'Z': 2})
    c = crawler(site)
    result = c.order('A', 2) if public else c._order_bare('A', 2)
    assert result.reason_code == 'not_sent' and result.no_retry
    assert site.basket == {'Z': 2} and mutations == [] and site.sends == []


@pytest.mark.parametrize('token,price', [('tZ', None), ('tZ', True), ('tZ', 12.75),
                                       ('', 1000), ('   ', 1000), ({'token': 'tZ'}, 1000)])
def test_tj_invalid_cached_restoration_metadata_is_not_trusted(token, price):
    site, mutations = metadata_site({'ItemCode': 'Z', 'InvQty': 9}, {'Z': 2})
    c = crawler(site)
    c._item_tokens['Z'], c._item_prices['Z'] = token, price
    result = c.order('A', 2)
    assert result.reason_code == 'not_sent' and result.no_retry
    assert site.basket == {'Z': 2} and mutations == [] and site.sends == []


@pytest.mark.parametrize('replacement', [
    {'ItemToken': {'token': 'bad'}, 'Cst': 1000}, {'ItemToken': ' ', 'Cst': 1000},
    {'ItemToken': 'tA'}, {'ItemToken': 'tA', 'Cst': 12.75}, {'ItemToken': 'tA', 'Cst': True}])
def test_tj_presend_search_cannot_overwrite_valid_original_metadata(replacement):
    site = Site({'A': 9}, basket={'A': 2})
    original = site.post
    catalog_reads = []
    def post(url, **kwargs):
        if url.endswith('item_api.php'):
            catalog_reads.append(kwargs['data']['name'])
            if len(catalog_reads) > 1:
                return Resp(json.dumps({'ResultSet': [{'ItemCode': 'A', 'InvQty': 9, **replacement}]}))
        return original(url, **kwargs)
    site.post = post
    c = crawler(site)
    result = c.order('A', 2)
    assert result.reason_code == 'not_sent' and result.no_retry
    assert site.sends == [] and site.basket == {'A': 2}
    assert c._item_tokens['A'] == 'tA' and c._item_prices['A'] == 1000
    assert c.cart_snapshot.load() is None


@pytest.mark.parametrize('prices,expected', [({'Cst': 0}, 0), ({'Cst': '0'}, 0),
    ({'Cst': 0, 'HiCst': 1234}, 1234), ({'Cst': None, 'HiCst': '1234'}, 1234),
    ({'Cst': '', 'HiCst': 1234}, 1234), ({'HiCst': 1234}, 1234)])
def test_tj_explicit_zero_and_known_insurance_price_restore(prices, expected):
    site, mutations = metadata_site({'ItemCode': 'Z', 'InvQty': 9, 'ItemToken': 'tZ', **prices}, {'Z': 2})
    result = crawler(site).order('A', 2)
    assert result.success and site.basket == {'Z': 2}
    restore = [request for request in mutations if request.get('ItemCode') == 'Z']
    assert restore[-1]['Cst'] == str(expected)


@pytest.mark.parametrize('token,price', [({}, 1000), (' ', 1000), ('tA', None), ('tA', True), ('tA', 12.75)])
def test_tj_add_boundary_rejects_invalid_metadata_without_post(token, price):
    site, mutations = metadata_site({'ItemCode': 'A', 'InvQty': 9, 'ItemToken': 'tA', 'Cst': 1000})
    c = crawler(site)
    c._item_tokens['A'], c._item_prices['A'] = token, price
    with pytest.raises(tj.BasketReadError):
        c._cart_add_raw('A', 2)
    assert mutations == []


@pytest.mark.parametrize('prices,expected', [({'Cst': 1000.0}, 1000),
    ({'Cst': 0.0, 'HiCst': 1234.0}, 1234), ({'Cst': '   ', 'HiCst': 1234.0}, 1234)])
def test_tj_whole_numeric_prices_preserve_existing_contract(prices, expected):
    site, mutations = metadata_site({'ItemCode': 'Z', 'InvQty': 9, 'ItemToken': 'tZ', **prices}, {'Z': 2})
    result = crawler(site).order('A', 2)
    assert result.success and site.basket == {'Z': 2}
    assert [request for request in mutations if request.get('ItemCode') == 'Z'][-1]['Cst'] == str(expected)


def test_tj_invalid_cached_metadata_can_be_repaired_from_known_catalog():
    site = Site({'A': 9, 'Z': 9}, basket={'Z': 2})
    c = crawler(site)
    c._item_tokens['Z'], c._item_prices['Z'] = {'bad': 'token'}, None
    assert c.order('A', 2).success and site.basket == {'Z': 2}
    assert c._item_tokens['Z'] == 'tZ' and c._item_prices['Z'] == 1000


@pytest.mark.parametrize('token,price', [({}, 1000), (' ', 1000), ('tA', None), ('tA', True), ('tA', 12.75)])
def test_tj_direct_add_boundary_rejects_invalid_metadata_without_post(token, price):
    site, mutations = metadata_site({'ItemCode': 'A', 'InvQty': 9, 'ItemToken': 'tA', 'Cst': 1000})
    with pytest.raises(tj.BasketReadError):
        crawler(site)._add_to_basket('A', price, 2, token)
    assert mutations == []
