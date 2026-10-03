# tests/test_beakje_order.py
"""백제 — session.get/post/delete 만 대역."""
import importlib.util
import json
import os
import sys
sys.path.insert(0, "src")

import fakeredis

from domae_mcp.core.crawlers.cart_snapshot import CartSnapshot

PATH = os.environ.get("BEAKJE_PY", os.path.expanduser(
    "~/.config/superpowers/worktrees/pharmsquare-server-main/order-resilience/prisma/seeds/domae-crawlers/beakje.py"))
spec = importlib.util.spec_from_file_location("beakje_under_test", PATH)
bj = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bj)


class Resp:
    def __init__(self, text, status=200):
        self.text, self.status_code = text, status


class Site:
    def __init__(self, stock, basket=None, token_ok=True, delete_works=True, accept=True, reply=200,
                 lose_reply=False, corrupt_basket=False):
        self.stock, self.basket = dict(stock), dict(basket or {})
        self.token_ok, self.delete_works, self.accept = token_ok, delete_works, accept
        self.reply, self.lose_reply, self.corrupt_basket = reply, lose_reply, corrupt_basket
        self.sends = []

    def get(self, url, params=None, headers=None, **k):
        if url.endswith("/ord/basketList"):
            if not self.token_ok:
                return Resp("2.유효한 인증토큰이 존재하지 않습니다.", 401)
            rows = [{"ITEM_CD": p.split("|")[0], "ITEM_GB_CD": p.split("|")[1], "ITEM_QTY": q}
                    for p, q in self.basket.items()]
            if self.corrupt_basket and rows:
                rows[0]["ITEM_QTY"] = None
            return Resp(json.dumps(rows))
        if url.endswith("/ord/itemSearch"):
            rows = [{"MAKER_NM": "m", "ITEM_NM": "n", "UNIT": "u", "AVAIL_STOCK": s, "BOHUM_CD": "1",
                     "ITEM_CD": p.split("|")[0], "ITEM_GB_CD": p.split("|")[1], "ORD_WP2_AMT": 100}
                    for p, s in self.stock.items()]
            return Resp(json.dumps(rows))
        raise AssertionError(url)

    def post(self, url, json=None, headers=None, **k):
        if url.endswith("/ord/addBasket"):
            p = f"{json['saveItemCd']}|{json['saveItemGbCd']}"
            self.basket[p] = self.basket.get(p, 0) + int(json["saveItemQty"])
            return Resp("{}")
        if url.endswith("/ord/orderReg"):
            for row in json:                                 # 실제 POST 본문을 접수 데이터로 쓴다
                assert {"ITEM_CD", "ITEM_GB_CD", "ITEM_QTY", "ORD_MEMO", "BRCH_CD", "CUST_CD", "DEPT_CD", "EMP_ID",
                        "USER_ID"} <= set(row), row
            self.sends.append({f"{r['ITEM_CD']}|{r['ITEM_GB_CD']}": int(r["ITEM_QTY"]) for r in json})
            if self.accept:
                self.basket = {}
            if self.lose_reply:
                raise TimeoutError("응답 유실")
            return Resp("{}", self.reply if self.accept else 500)
        raise AssertionError(url)

    def delete(self, url, params=None, headers=None, **k):
        if self.delete_works:
            self.basket.pop(f"{params['saveItemCd']}|{params['saveItemGbCd']}", None)
        return Resp("{}", 200 if self.delete_works else 500)


def crawler(site):
    c = bj.BeakjeCrawler.__new__(bj.BeakjeCrawler)
    c.session, c._cust_cd, c._login_id, c._login_pw, c._logged_in = site, "C", "", "", True
    c._auth_headers = lambda: {}
    c.ensure_login = lambda *a: True
    c.login = lambda *a: site.token_ok
    c.cart_snapshot = CartSnapshot(fakeredis.FakeRedis(), "m", "백제")
    return c

def leftover(c, snap):
    """이전 실행이 남긴 기록(잠금은 이미 풀림)."""
    old = CartSnapshot(c.cart_snapshot._r, "m", c.cart_snapshot._s)
    old.lock()
    old.save(snap)
    old.unlock()



def test_bj_untrusted_keeps_cart():
    site = Site({"A|01": 9}, basket={"Z|01": 3}, token_ok=False)
    assert crawler(site)._order_bare("A|01", 2).reason_code == "not_sent" and site.sends == [] and site.basket == {"Z|01": 3}


def test_bj_corrupt_row_sends_nothing():
    site = Site({"A|01": 9}, basket={"Z|01": 3}, corrupt_basket=True)
    assert crawler(site)._order_bare("A|01", 2).reason_code == "not_sent" and site.sends == []


def test_bj_clear_failure_sends_nothing():
    site = Site({"A|01": 9}, basket={"Z|01": 3}, delete_works=False)
    assert crawler(site)._order_bare("A|01", 2).reason_code == "not_sent" and site.sends == []


def test_bj_success_restores_cart():
    site = Site({"A|01": 9}, basket={"Z|01": 3})
    c = crawler(site)
    assert c._order_bare("A|01", 2).success and site.sends == [{"A|01": 2}] and site.basket == {"Z|01": 3}
    assert c.cart_snapshot.load() is None


def test_bj_lost_reply_is_unknown_not_resent():
    site = Site({"A|01": 9}, lose_reply=True)
    assert crawler(site).order("A|01", 5, product_name="n").reason_code == "send_unknown" and len(site.sends) == 1


def test_bj_non2xx_is_unknown_not_rejected():
    site = Site({"A|01": 9}, accept=False)
    assert crawler(site).order("A|01", 5, product_name="n").reason_code == "send_unknown" and len(site.sends) == 1


def test_bj_presend_stock_adjust():
    site = Site({"A|01": 2, "B|01": 0})
    res = crawler(site).order_batch([{"product_id": "A|01", "quantity": 5, "product_name": "n"},
                                     {"product_id": "B|01", "quantity": 1, "product_name": "n"}])
    assert site.sends == [{"A|01": 2}] and res[0].adjusted_quantity == 2 and res[1].reason_code == "stock_zero"


def test_bj_pending_snapshot_blocks():
    site = Site({"A|01": 9}, basket={"Y|01": 1})
    c = crawler(site)
    leftover(c, {"Z|01": 3})
    r = c._order_bare("A|01", 2)
    assert r.reason_code == "not_sent" and r.no_retry and site.sends == [] and site.basket == {"Y|01": 1}


def test_bj_add_timeout_restores_and_not_sent():
    site = Site({"A|01": 9}, basket={"Z|01": 3})
    orig = site.post

    def post(url, json=None, **kw):
        if url.endswith("/ord/addBasket") and json["saveItemCd"] == "A":
            raise TimeoutError("담기 응답 없음")
        return orig(url, json=json, **kw)
    site.post = post
    r = crawler(site)._order_bare("A|01", 2)
    assert r.reason_code == "not_sent" and site.sends == [] and site.basket == {"Z|01": 3}


def test_bj_failed_delete_is_not_success():
    site = Site({"A|01": 9}, accept=False)
    c = crawler(site)
    orig = site.post

    def post(url, json=None, **kw):
        res = orig(url, json=json, **kw)
        if url.endswith("/ord/orderReg"):
            site.delete_works = False                     # 복원 단계 삭제가 실패
        return res
    site.post = post
    r = c._order_bare("A|01", 2)
    assert r.reason_code == "send_unknown" and c.cart_snapshot.load() is not None
    assert c.cart_snapshot._r.get(c.cart_snapshot.failed_key)


def test_bj_send_guard_blocks_send():
    site = Site({"A|01": 9})
    c = crawler(site)

    def guard():
        raise RuntimeError("소유권 상실")
    c.send_guard = guard
    assert c._order_bare("A|01", 2).reason_code == "not_sent" and site.sends == []


def test_bj_search_does_not_merge_different_pids():
    rows = crawler(Site({"P|01": 2, "P|02": 9})).search("n")
    assert sorted((r.product_id, r.quantity) for r in rows) == [("P|01", 2), ("P|02", 9)]

import copy
import pytest


@pytest.mark.parametrize('field,value', [('ITEM_CD', ''), ('ITEM_CD', True), ('ITEM_CD', []),
    ('ITEM_CD', 'A|B'), ('ITEM_GB_CD', {}), ('ITEM_GB_CD', ' '),
    ('ITEM_QTY', True), ('ITEM_QTY', 1.5), ('ITEM_QTY', '1.5'), ('ITEM_QTY', 0), ('ITEM_QTY', -1)])
def test_invalid_basket_fields_never_mutate(field, value):
    site = Site({'A|01': 9}, {'Z|01': 3})
    orig = site.get
    def get(url, **kw):
        resp = orig(url, **kw)
        if url.endswith('/ord/basketList'):
            rows = json.loads(resp.text)
            rows[0][field] = value
            return Resp(json.dumps(rows))
        return resp
    site.get = get
    r = crawler(site)._order_bare('A|01', 2)
    assert r.reason_code == 'not_sent'
    assert site.basket == {'Z|01': 3} and not site.sends


@pytest.mark.parametrize('quantity', [True, 1.5, '1.5', 0, -1, None])
def test_invalid_requested_quantity_does_not_order(quantity):
    site = Site({'A|01': 9}, {'Z|01': 3})
    r = crawler(site).order('A|01', quantity, product_name='n')
    assert r.reason_code == 'not_sent' and not site.sends
    assert site.basket == {'Z|01': 3}


def test_payload_preserves_raw_fields_and_numeric_representations():
    site = Site({'A|01': 9})
    get, post = site.get, site.post
    bodies = []
    def enriched(url, **kw):
        resp = get(url, **kw)
        if url.endswith('/ord/basketList'):
            rows = json.loads(resp.text)
            for row in rows:
                row.update(ITEM_QTY=str(row['ITEM_QTY']), PRICE='123.00', BRCH_CD='branch', CUSTOM={'keep': True})
            return Resp(json.dumps(rows))
        return resp
    def capture(url, json=None, **kw):
        if url.endswith('/ord/orderReg'):
            bodies.append(copy.deepcopy(json))
        return post(url, json=json, **kw)
    site.get, site.post = enriched, capture
    assert crawler(site).order('A|01', 2, product_name='n').success
    assert bodies[0][0]['ITEM_QTY'] == '2'
    assert bodies[0][0]['CUSTOM'] == {'keep': True}
    assert bodies[0][0]['PRICE'] == '123.00' and bodies[0][0]['BRCH_CD'] == 'branch'


@pytest.mark.parametrize('failure', ['mismatch', 'read', 'parse'])
def test_final_raw_body_failure_freezes_even_when_next_read_succeeds(failure):
    site = Site({'A|01': 9}, {'Z|01': 3})
    c = crawler(site)
    get = site.get
    armed = [False]
    def guard():
        armed[0] = True
    c.send_guard = guard
    def fail_body(url, **kw):
        if url.endswith('/ord/basketList') and armed[0]:
            armed[0] = False
            if failure == 'read':
                raise TimeoutError('secret-token-in-exception')
            if failure == 'parse':
                return Resp('[{"ITEM_CD":"A","ITEM_GB_CD":"01","ITEM_QTY":true}]')
            return Resp('[{"ITEM_CD":"X","ITEM_GB_CD":"01","ITEM_QTY":2}]')
        return get(url, **kw)
    site.get = fail_body
    r = c._order_bare('A|01', 2)
    assert r.reason_code == 'not_sent' and r.no_retry and not site.sends
    assert site.basket == {'A|01': 2}
    assert c.cart_snapshot.load() is not None
    assert 'secret-token' not in r.message


def test_pharmacist_change_after_first_delete_stops_second_delete():
    site = Site({'A|01': 9}, {'Z|01': 3, 'Y|01': 4})
    delete = site.delete
    deletes = []
    def race(url, params=None, **kw):
        deletes.append(params['saveItemCd'])
        resp = delete(url, params=params, **kw)
        site.basket['NEW|01'] = 8
        return resp
    site.delete = race
    c = crawler(site)
    assert c._order_bare('A|01', 2).reason_code == 'not_sent'
    assert deletes == ['Z'] and site.basket == {'Y|01': 4, 'NEW|01': 8} and not site.sends


def test_duplicate_product_uses_one_stock_budget_and_confirmed_quantities():
    site = Site({'A|01': 5})
    rows = crawler(site).order_batch([{'product_id': 'A|01', 'quantity': 4, 'product_name': 'n'},
                                    {'product_id': 'A|01', 'quantity': 4, 'product_name': 'n'}])
    assert site.sends == [{'A|01': 5}]
    assert [(r.fulfilled_quantity, r.adjusted_quantity, r.original_quantity) for r in rows] == [(4, 4, 4), (1, 1, 4)]


def test_retained_requested_basket_is_warning_and_2xx_is_accepted(caplog):
    site = Site({'A|01': 2})
    post = site.post
    def retain(url, **kw):
        resp = post(url, **kw)
        if url.endswith('/ord/orderReg'):
            site.basket = {'A|01': 2}
            resp.status_code = 201
        return resp
    site.post = retain
    assert crawler(site)._order_bare('A|01', 2).success
    assert len(site.sends) == 1 and '장바구니' in caplog.text


def test_unknown_does_not_claim_confirmed_quantity():
    site = Site({'A|01': 2}, lose_reply=True)
    r = crawler(site).order('A|01', 5, product_name='n')
    assert r.reason_code == 'send_unknown' and r.no_retry
    assert r.fulfilled_quantity == 0 and r.adjusted_quantity is None and r.original_quantity == 5
    assert len(site.sends) == 1


def test_insurance_first_and_exact_identity():
    site = Site({'OTHER|01': 99})
    get = site.get
    keywords = []
    def search(url, params=None, **kw):
        if url.endswith('/ord/itemSearch'):
            keywords.append(params['keyword'])
        return get(url, params=params, **kw)
    site.get = search
    r = crawler(site).order('A|01', 2, product_name='n', insurance_code='123456789', memo='memo')
    assert keywords[0] == '123456789' and r.reason_code == 'not_sent' and not site.sends


def test_search_numeric_stocks_and_distinct_codes():
    site = Site({'A|01': '2', 'B|01': '9'})
    get = site.get
    def duplicate(url, **kw):
        resp = get(url, **kw)
        if url.endswith('/ord/itemSearch'):
            rows = json.loads(resp.text)
            rows.append(dict(rows[0], AVAIL_STOCK='3'))
            return Resp(json.dumps(rows))
        return resp
    site.get = duplicate
    assert sorted((r.product_id, r.quantity) for r in crawler(site).search('n')) == [('A|01', 5), ('B|01', 9)]


def test_guard_rechecked_after_body_read():
    site = Site({'A|01': 9})
    c = crawler(site)
    checks = []
    def guard():
        checks.append(1)
        if len(checks) == 2:
            raise RuntimeError('lease lost')
    c.send_guard = guard
    assert c._order_bare('A|01', 2).reason_code == 'not_sent'
    assert len(checks) == 2 and not site.sends


@pytest.mark.parametrize('rows', [{}, None, [None], [True], ['bad']])
def test_malformed_basket_container_never_mutates(rows):
    site = Site({'A|01': 9}, {'Z|01': 3})
    get = site.get
    site.get = lambda url, **kw: Resp(json.dumps(rows)) if url.endswith('/ord/basketList') else get(url, **kw)
    r = crawler(site)._order_bare('A|01', 2)
    assert r.reason_code == 'not_sent' and site.basket == {'Z|01': 3} and not site.sends


def test_lock_loss_during_final_body_read_freezes():
    site = Site({'A|01': 9}, {'Z|01': 3})
    c = crawler(site)
    get = site.get
    armed = [False]
    c.send_guard = lambda: armed.__setitem__(0, True)
    def lose_lock(url, **kw):
        response = get(url, **kw)
        if url.endswith('/ord/basketList') and armed[0]:
            c.cart_snapshot._r.set(c.cart_snapshot.lock_key, 'another-run')
        return response
    site.get = lose_lock
    r = c._order_bare('A|01', 2)
    assert r.reason_code == 'not_sent' and not site.sends
    assert site.basket == {'A|01': 2} and c.cart_snapshot.load() is not None


def test_cart_change_by_guard_blocks_send_and_preserves_pharmacist_cart():
    site = Site({'A|01': 9}, {'Z|01': 3})
    c = crawler(site)
    def change():
        site.basket['NEW|01'] = 1
    c.send_guard = change
    assert c._order_bare('A|01', 2).reason_code == 'not_sent'
    assert not site.sends and site.basket == {'A|01': 2, 'NEW|01': 1}


@pytest.mark.parametrize('value', [True, {}, [], '', ' ', None])
def test_malformed_search_identity_is_skipped(value):
    site = Site({'A|01': 9})
    get = site.get
    def malformed(url, **kw):
        response = get(url, **kw)
        if url.endswith('/ord/itemSearch'):
            rows = json.loads(response.text)
            rows[0]['ITEM_CD'] = value
            return Resp(json.dumps(rows))
        return response
    site.get = malformed
    assert crawler(site).search('n') == []


def test_search_lossless_numeric_identifiers_preserved():
    site = Site({})
    site.get = lambda *a, **kw: Resp(json.dumps([
        {'MAKER_NM': 'm', 'ITEM_NM': 'n', 'UNIT': 'u', 'ITEM_CD': 123.5, 'ITEM_GB_CD': 1,
         'AVAIL_STOCK': '2', 'BOHUM_CD': '1', 'ORD_WP2_AMT': 100}]))
    rows = crawler(site).search('n')
    assert [(r.product_id, r.quantity) for r in rows] == [('123.5|1', 2)]


@pytest.mark.parametrize('stock', [True, 1.5, '1.5', None, {}, -1])
def test_invalid_search_stock_cannot_be_used_for_order(stock):
    site = Site({'A|01': stock})
    r = crawler(site).order('A|01', 2, product_name='n')
    assert r.reason_code == 'not_sent' and not site.sends


@pytest.mark.parametrize('failure', ['read', 'foreign'])
def test_postresponse_untrusted_cart_freezes_restoration(failure):
    site = Site({'A|01': 9}, {'Z|01': 3})
    c = crawler(site)
    get, post = site.get, site.post
    armed = [False]
    def send(url, **kw):
        response = post(url, **kw)
        if url.endswith('/ord/orderReg'):
            armed[0] = True
        return response
    def uncertain(url, **kw):
        if url.endswith('/ord/basketList') and armed[0]:
            armed[0] = False
            if failure == 'read':
                raise TimeoutError('temporary read loss')
            return Resp('[{"ITEM_CD":"NEW","ITEM_GB_CD":"01","ITEM_QTY":1}]')
        return get(url, **kw)
    site.get, site.post = uncertain, send
    r = c._order_bare('A|01', 2)
    assert r.success and len(site.sends) == 1
    assert site.basket == {} and c.cart_snapshot.load() is not None
