"""검수 지적 A1·A4·A5·A6·C2·C3 회귀 — 워커 스케줄러/대체주문.

DB·Redis·크롤러는 메모리 대역이다. SQL 문자열과 커밋 순서로 계약을 검증한다.
"""
import sys
sys.path.insert(0, "src")

import json
import pytest
from domae_mcp.cloud import scheduler as sch
from domae_mcp.cloud import fallback as fb
from domae_mcp.core.crawlers.base import OrderResult, SearchResult


class Conn:
    """execute 를 기록하고, 커밋 시점까지의 문장을 committed 로 옮긴다."""
    def __init__(self, creds_row=None, counts=(0, 0, 0), fail_on=None, unconfirmed_rows=()):
        self.sql, self.pending, self.committed = [], [], []
        self.creds_row = creds_row
        self.counts = counts
        self.fail_on = fail_on
        self.unconfirmed_rows = list(unconfirmed_rows)
        self.rowcount = 1
        self.commits = self.rollbacks = 0
        self.last = ''

    def cursor(self): return self

    def execute(self, sql, params=None):
        self.last = ' '.join(sql.split())
        if self.fail_on and self.fail_on in self.last:
            raise RuntimeError('db down')
        self.sql.append((self.last, params))
        self.pending.append((self.last, params))

    def fetchone(self):
        if 'RETURNING id' in self.last: return ('b',)
        if 'FILTER' in self.last and 'domae_cloud_orders' in self.last: return self.counts
        if 'SELECT count(*)' in self.last: return (0,)
        if 'SELECT m.credentials' in self.last: return self.creds_row
        return None

    def fetchall(self):
        if 'productId' in self.last: return list(self.unconfirmed_rows)
        return []

    def commit(self):
        self.commits += 1
        self.committed.extend(self.pending)
        self.pending = []

    def rollback(self):
        self.rollbacks += 1
        self.pending = []


class Pool:
    def __init__(self, *conns): self.conns = list(conns); self.returned = []
    def getconn(self): return self.conns.pop(0)
    def putconn(self, c): self.returned.append(c)


class Redis:
    def __init__(self): self.events = []
    def publish(self, k, v): self.events.append(json.loads(v))
    def set(self, *a, **k): return True
    def eval(self, *a, **k): return 1
    def get(self, *a): return None
    def delete(self, *a): return 1


ITEM = {'quantity': 15, 'price': 1000, 'cart_item_id': 'c', 'db_order_id': 'o',
        'insurance_code': '694003321', 'unit': '12EA', 'product_name': '베아놀', 'product_id': 'p'}


def make_crawler(log, results=None, login_ok=True, raises=None, search=None, order_result=None):
    class C:
        def login(self, *a):
            log.append(('login',)); return login_ok

        def search(self, kw):
            log.append(('search', kw)); return list(search or [])

        def order_batch(self, items):
            log.append(('order_batch', [i['product_id'] for i in items]))
            if raises: raise raises
            return results

        def order(self, pid, qty, **k):
            log.append(('order', pid, qty)); return order_result or OrderResult(success=True)
    return C


def scheduler_with(primary, secondary, crawlers, redis=None):
    pool, redis = Pool(primary, secondary), redis or Redis()
    s = sch.CloudScheduler(pool, redis)
    s._get_conn = pool.getconn
    s._decrypt_creds = lambda raw: raw
    s._crawlers_loaded = True
    s._crawlers = crawlers
    return s, redis


AUTO_CREDS = ({'인천': {'login_id': 'x'}, '복산': {'login_id': 'x'}}, 'chat', ['인천', '복산'], False)


def run_auto(results=None, raises=None, fail_on=None, counts=(0, 0, 0), fallback=False,
             fb_login=True, extra_crawlers=None):
    log, fblog = [], []
    primary = Conn((AUTO_CREDS[0], 'chat', AUTO_CREDS[2], fallback), counts=counts, fail_on=fail_on)
    secondary = Conn()
    crawlers = {'인천': make_crawler(log, results, raises=raises),
                '복산': make_crawler(fblog, login_ok=fb_login,
                                     search=[SearchResult('', '베아놀', '12EA', '694003321', 99, '복산', 900, 'bp')])}
    s, redis = scheduler_with(primary, secondary, crawlers)
    tg = []
    s._send_auto_order_telegram = lambda *a, **kw: tg.append((a, kw))
    seen = {}

    orig = crawlers['인천'].order_batch
    def spy(self, items):
        seen['committed_at_send'] = list(primary.committed)
        return orig(self, items)
    crawlers['인천'].order_batch = spy
    s.auto_order({'monitor_id': 'm', 'batch_id': 'b', 'supplier': '인천', 'items': [dict(ITEM)]})
    return primary, secondary, redis, tg, log, fblog, seen


def cart_marks(stmts):
    return [p for sql, p in stmts if sql.startswith('UPDATE domae_cart_items') and '전송 결과 확인 중' in str(p)]


# ── A1 ──────────────────────────────────────────────────────────────

def test_a1_auto_cart_row_marked_in_same_commit_before_send():
    primary, *_, seen = run_auto(raises=TimeoutError('응답 유실'))
    committed = seen['committed_at_send']
    marks = cart_marks(committed)
    assert marks, '전송 전에 장바구니 행 보호가 커밋돼야 한다'
    assert any(['c'] in (list(x) if isinstance(x, (list, tuple)) else [x]) or x == ['c'] for x in marks[0])
    # 주문행 보호와 같은 커밋 단위인지: 둘 다 전송 시점에 커밋돼 있어야 한다
    assert any('send_unknown' in str(p) and 'domae_cloud_orders' in sql for sql, p in committed)
    # 약국·도매 단위 일괄 갱신이 아니라 id 지정이어야 한다
    for sql, _ in committed:
        if sql.startswith('UPDATE domae_cart_items'):
            assert 'supplier' not in sql and '"monitorId"' not in sql


def test_a1_mark_sending_only_listed_ids():
    c = Conn()
    sch._mark_sending(c, c, 'b', '인천', ['c1', None, 'c2'])
    assert c.commits == 1
    stmts = [(s, p) for s, p in c.committed if s.startswith('UPDATE domae_cart_items')]
    assert len(stmts) == 1 and list(stmts[0][1][-1]) == ['c1', 'c2']


def test_a1_mark_sending_without_cart_ids_touches_no_cart():
    c = Conn()
    sch._mark_sending(c, c, 'b', '인천', [])
    assert not [s for s, _ in c.committed if 'domae_cart_items' in s]


# ── A4 ──────────────────────────────────────────────────────────────

def test_a4_mark_sending_failure_is_not_sent_and_no_send():
    primary, _, _, _, log, _, _ = run_auto(results=[OrderResult(success=True)],
                                           fail_on='SET "reasonCode" = %s, message = %s WHERE "batchId"')
    assert not [x for x in log if x[0] == 'order_batch']
    recs = [p for s, p in primary.sql if 'SET success = %s' in s]
    assert recs and recs[0][0] is False and recs[0][-2] == 'not_sent'


def test_a4_exception_after_commit_is_send_unknown():
    primary, *_ = run_auto(raises=TimeoutError('lost'), counts=(0, 0, 1))
    recs = [p for s, p in primary.sql if 'SET success = %s' in s]
    assert recs[0][0] is None and recs[0][-2] == 'send_unknown'


# ── A5 ──────────────────────────────────────────────────────────────

def batch_updates(conn):
    return [(s, p) for s, p in conn.sql if s.startswith('UPDATE domae_order_batches') and '"successCount"' in s]


def test_a5_auto_unconfirmed_keeps_processing():
    primary, _, redis, tg, *_ = run_auto(raises=TimeoutError('lost'), counts=(0, 0, 1))
    ups = batch_updates(primary)
    assert ups and 'completed' not in str(ups[-1][1])
    assert 'processing' in str(ups[-1][1]) and 'NULL' in ups[-1][0]
    logs = [p for s, p in primary.sql if s.startswith('UPDATE domae_auto_order_logs')]
    assert logs[-1][0] not in ('failed', 'partial_fail')
    assert redis.events[0]['status'] not in ('failed', 'partial_fail')


def test_a5_auto_counts_come_from_db_after_fallback():
    primary, *_ = run_auto(results=[OrderResult(success=True)], counts=(2, 1, 0))
    s, p = batch_updates(primary)[-1]
    assert 'completed' in str(p) and 2 in p and 1 in p


def test_a5_telegram_title_unconfirmed_is_not_failure(monkeypatch):
    from domae_mcp.cloud.notifier import Notifier
    s = sch.CloudScheduler(None, None)
    sent = []
    monkeypatch.setattr(Notifier, 'send_telegram', lambda *a, **kw: sent.append(a))
    s._send_auto_order_telegram('chat', '인천', [], [], unconfirmed_items=[{'product_name': '베아놀', 'quantity': 1}])
    assert '실패' not in sent[0][1].splitlines()[0]
    assert '확인' in sent[0][1].splitlines()[0]


# ── A6 ──────────────────────────────────────────────────────────────

def test_a6_fallback_login_failure_does_not_search_or_order():
    *_, fblog, _ = run_auto(results=[OrderResult(success=False, reason_code='stock_zero')],
                            fallback=True, fb_login=False)
    assert ('login',) in fblog
    assert not [x for x in fblog if x[0] in ('search', 'order', 'order_batch')]


# ── C2 ──────────────────────────────────────────────────────────────

def sr(sup, pid='p1'):
    return SearchResult('', '베아놀', '12EA', '694003321', 50, sup, 1000, pid)


class FC:
    def __init__(self, sup, sync=False):
        self.sup, self.orders = sup, []
        self.SUPPORTS_CART_SYNC = sync

    def search(self, kw): return [sr(self.sup)]

    def order(self, pid, qty, **k):
        self.orders.append(pid); return OrderResult(success=True)


def run_fb(crawlers, check):
    pend = []
    out = fb.run_fallback(
        [({'insurance_code': '694003321', 'unit': '12EA', 'quantity': 5}, 5)], list(crawlers),
        lambda s: crawlers[s], lambda s: 'nolock', lambda *a: True, lambda *a: None,
        lambda it, s, pick, q: pend.append(s) or 'row', lambda *a: None, lambda *a: None,
        check_unconfirmed=check)
    return out[0], pend


def test_c2_same_product_unconfirmed_blocks_item():
    cs = {'복산': FC('복산'), '백제': FC('백제')}
    o, pend = run_fb(cs, lambda s, pid: 'same_product' if s == '복산' else None)
    assert o.state == 'blocked' and '이전 주문 결과 확인 전' in o.message
    assert pend == [] and cs['복산'].orders == [] and cs['백제'].orders == []


def test_c2_cart_sync_supplier_any_unconfirmed_skips_to_next():
    cs = {'복산': FC('복산', sync=True), '백제': FC('백제')}
    o, pend = run_fb(cs, lambda s, pid: 'other_product' if s == '복산' else None)
    assert o.state == 'ordered' and o.supplier == '백제' and cs['복산'].orders == []


def test_c2_non_sync_other_product_unconfirmed_proceeds():
    cs = {'백제': FC('백제')}
    o, _ = run_fb(cs, lambda s, pid: 'other_product')
    assert o.state == 'ordered'


def test_c2_check_failure_sends_nothing_there():
    cs = {'복산': FC('복산'), '백제': FC('백제')}
    def check(s, pid):
        if s == '복산': raise RuntimeError('db')
        return None
    o, pend = run_fb(cs, check)
    assert cs['복산'].orders == [] and o.supplier == '백제'


def test_c2_blocked_is_treated_as_needs_check():
    o = fb.FallbackOutcome({}, 5, '복산', 0, 'blocked', '이전 주문 결과 확인 전 — 대체 주문 중단')
    assert fb.cart_action_after_fallback(o)[0] == 'note'


def test_c2_recorder_check_sql():
    from domae_mcp.cloud.fallback_db import FallbackRecorder
    c = Conn(unconfirmed_rows=[('p1',)])
    r = FallbackRecorder(c, 'm', 'b', '인천', None, None, None)
    assert r.check_unconfirmed('복산', 'p1') == 'same_product'
    assert r.check_unconfirmed('복산', 'p2') == 'other_product'
    sql, params = c.sql[0]
    assert 'success IS NULL' in sql and '"monitorId"' in sql and params[:2] == ('m', '복산')
    c2 = Conn()
    assert FallbackRecorder(c2, 'm', 'b', '인천', None, None, None).check_unconfirmed('복산', 'p1') is None


# ── C3 / batch_order ───────────────────────────────────────────────

def run_batch(results=None, raises=None, fail_on=None, counts=(0, 0, 0)):
    log = []
    primary = Conn(({'인천': {'login_id': 'x'}}, None), counts=counts, fail_on=fail_on)
    s, _ = scheduler_with(primary, Conn(), {'인천': make_crawler(log, results, raises=raises)})
    seen = {}
    orig = s._crawlers['인천'].order_batch
    def spy(self, items):
        seen['committed_at_send'] = list(primary.committed)
        return orig(self, items)
    s._crawlers['인천'].order_batch = spy
    s.batch_order({'monitor_id': 'm', 'batch_id': 'b', 'items': [dict(ITEM, supplier='인천')]})
    return primary, log, seen


def test_c3_batch_partial_success_keeps_shortfall(monkeypatch):
    monkeypatch.setattr(sch.time, 'sleep', lambda *_: None)
    primary, *_ = run_batch([OrderResult(success=True, reason_code='stock_adjusted', adjusted_quantity=10)])
    assert not [s for s, _ in primary.sql if s.startswith('DELETE FROM domae_cart_items')]
    keep = [p for s, p in primary.sql if s.startswith('UPDATE domae_cart_items') and 'quantity' in s]
    assert keep and keep[-1][0] == 5


def test_a1_batch_cart_row_marked_before_send(monkeypatch):
    monkeypatch.setattr(sch.time, 'sleep', lambda *_: None)
    _, _, seen = run_batch(raises=TimeoutError('lost'), counts=(0, 0, 1))
    assert cart_marks(seen['committed_at_send'])


def test_a4_batch_mark_failure_is_not_sent(monkeypatch):
    monkeypatch.setattr(sch.time, 'sleep', lambda *_: None)
    primary, log, _ = run_batch([OrderResult(success=True)], fail_on='SET "reasonCode" = %s, message = %s WHERE "batchId"')
    assert not [x for x in log if x[0] == 'order_batch']
    recs = [p for s, p in primary.sql if 'SET success = %s' in s]
    assert recs and recs[0][0] is False and recs[0][-2] == 'not_sent'


def test_a5_batch_unconfirmed_keeps_processing_and_not_failed(monkeypatch):
    monkeypatch.setattr(sch.time, 'sleep', lambda *_: None)
    primary, *_ = run_batch(raises=TimeoutError('lost'), counts=(0, 0, 1))
    s, p = batch_updates(primary)[-1]
    assert p[0] == 'processing' and p[2] == 0 and '"completedAt" = NULL' in s   # 미확정은 실패로 세지 않는다
