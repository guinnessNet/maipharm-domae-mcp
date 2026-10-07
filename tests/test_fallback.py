# tests/test_fallback.py
import sys
sys.path.insert(0, "src")
import pytest

from domae_mcp.core.crawlers.base import OrderResult, SearchResult
from domae_mcp.cloud.fallback import (
    FallbackOutcome, cart_action_after_fallback, cart_action_after_order, fallback_need_qty,
    format_ordered_line, next_suppliers, pack_signature, pick_candidate, run_fallback,
)


def sr(sup, pid, ic, unit, qty, price=1000):
    return SearchResult(maker="", product_name=f"{sup}-{pid}", unit=unit, insurance_code=ic,
                        quantity=qty, supplier=sup, price=price, product_id=pid)


def test_pack_signature():
    s = pack_signature
    assert s("20mL*12P") == s("20ml*12포") == s("20ml/12P(포)") == ("12p", "20ml")
    assert s("20ml*12P") != s("15ml*12P")                       # 용량이 다르면 다른 포장
    assert s("5/500T") == s("500T(병)") == s("500T") == ("500t",)  # 복산 함량 접두 '5/'
    assert s("1.5/1000T") == ("1000t",)
    assert s("100T") != s("1000T")
    assert s("200/200g") == s("200G") == s("200g(병)") == ("200g",)
    assert s("28정") == ("28t",) and s("180C(PTP)") == ("180c",)
    assert s("1EA") != s("500ml")
    assert s("0.3ml*12EA") != s("12EA")
    assert s("100T(2병)") is None                               # 숫자 있는 괄호는 비교 불가
    assert s("100P(20P*5EA)") is None
    assert s("28T x2") is None                                  # 해석 못 한 숫자가 남으면 제외
    assert s("0.15%/0.4ml") is None
    assert s("") is None and s(None) is None and s("병") is None


def test_fallback_need_qty():
    it = {"quantity": 15}
    assert fallback_need_qty(it, OrderResult(success=False, reason_code="stock_zero")) == 15
    assert fallback_need_qty(it, OrderResult(success=True, reason_code="stock_adjusted",
                                             adjusted_quantity=10)) == 5
    for rc in ("isolated_fail", "not_sent", "send_unknown", "rejected", "other", None):
        assert fallback_need_qty(it, OrderResult(success=False, reason_code=rc)) == 0
    assert fallback_need_qty(it, OrderResult(success=True, reason_code="ok")) == 0


def test_next_suppliers():
    order = ["인천", "복산", "티제이팜", "지오영", "백제"]
    assert next_suppliers(order, "인천", {"복산", "백제"}) == ["복산", "백제"]
    assert next_suppliers(order, "백제", {"인천"}) == []
    assert next_suppliers([], "인천", {"복산"}) == []           # 순번 미저장이면 대체 없음
    assert next_suppliers(order, "없는도매", {"복산"}) == []


def test_pack_mismatch_is_rejected():
    rs = [sr("복산", "p1", "642102300", "1000T", 50), sr("복산", "p2", "642102300", "100T", 5)]
    assert pick_candidate(rs, "642102300", "100T", 10) is None
    assert pick_candidate(rs, "642102300", "100T", 5).product_id == "p2"
    assert pick_candidate([sr("백제", "b", "643304081", "1EA", 9)], "643304081", "500ml", 1) is None
    assert pick_candidate([sr("복산", "x", "642102282", "15ml*12P", 99)], "642102282", "20ml*12P", 1) is None
    assert pick_candidate([sr("복산", "y", "642102300", "100T(2병)", 99)], "642102300", "100T", 1) is None


def test_pick_requires_code_and_prefers_most_stock():
    rs = [sr("복산", "a", "694003321", "12EA", 20), sr("복산", "b", "694003321", "12EA", 80),
          sr("복산", "c", "000000000", "12EA", 999)]
    assert pick_candidate(rs, "694003321", "12EA", 15).product_id == "b"
    assert pick_candidate(rs, "", "12EA", 15) is None


class FakeCrawler:
    def __init__(self, results, order_result=None, order_raises=False):
        self.results, self.order_result, self.order_raises = results, order_result, order_raises
        self.orders = []

    def search(self, kw):
        return self.results

    def order(self, pid, qty, **kw):
        self.orders.append((pid, qty))
        if self.order_raises:
            raise TimeoutError("응답 없음")
        return self.order_result


ITEM = {"product_name": "베아놀", "insurance_code": "694003321", "unit": "12EA", "quantity": 15}


def _run(crawlers, locks=None, renew_ok=None, pending_raises=(), result_raises=(), calls=None):
    calls = calls if calls is not None else []

    def pending(it, s, pick, q):
        if s in pending_raises:
            raise RuntimeError("db down")
        calls.append(("pending", s))
        return f"row-{s}"

    def result(row, it, s, r):
        if s in result_raises:
            raise RuntimeError("db down")
        calls.append(("result", s, r.success, r.reason_code))

    def unconfirmed(row, msg, confirmed=None):
        calls.append(("unconfirmed", row))

    out = run_fallback(
        [(dict(ITEM), 15)], list(crawlers),
        open_crawler=lambda s: crawlers[s],
        acquire_lock=lambda s: (locks or {}).get(s, "nolock"),
        renew_lock=lambda s, t: (renew_ok or {}).get(s, True),
        release_lock=lambda s, t: calls.append(("release", s)),
        record_pending=pending, record_result=result, record_unconfirmed=unconfirmed,
    )
    return out[0], calls


def _ok(sup):
    return FakeCrawler([sr(sup, sup + "1", "694003321", "12EA", 30)], OrderResult(success=True))


@pytest.mark.parametrize('fulfilled', [0, 2, 4, True])
def test_positive_adjusted_failure_stops_fallback_and_preserves_receipt(fulfilled):
    first, second = _ok('복산'), _ok('백제')
    first.order_result = OrderResult(reason_code='not_sent', adjusted_quantity=3, fulfilled_quantity=fulfilled)
    outcome, calls = _run({'복산': first, '백제': second})
    assert not second.orders and len(first.orders) == 1
    assert outcome.state == 'unconfirmed'
    assert outcome.ordered_qty == (4 if type(fulfilled) is int and fulfilled == 4 else 3)


@pytest.mark.parametrize('adjusted', [True, 1.5, '3', -1, 16, 0])
def test_invalid_success_adjustment_stops_fallback_before_recording_raw_quantity(adjusted):
    first, second = _ok('복산'), _ok('백제')
    first.order_result = OrderResult(success=True, adjusted_quantity=adjusted, fulfilled_quantity=3)
    outcome, calls = _run({'복산': first, '백제': second})
    assert not second.orders and outcome.state == 'unconfirmed' and outcome.ordered_qty == 3
    assert first.order_result.adjusted_quantity is None
    assert not any(call[0] == 'result' for call in calls)


def test_success_on_first_candidate():
    c = _ok("복산")
    o, calls = _run({"복산": c})
    assert o.state == "ordered" and o.supplier == "복산" and o.ordered_qty == 15
    assert c.orders == [("복산1", 15)] and ("release", "복산") in calls


def test_pending_recorded_before_order():
    calls = []
    c = _ok("복산")
    original = c.order
    seen = []

    def order(pid, qty, **kw):
        seen.append(list(calls))
        return original(pid, qty, **kw)

    c.order = order
    _run({"복산": c}, calls=calls)
    assert ("pending", "복산") in seen[0]


def test_lock_unavailable_skips_supplier():
    a, b = _ok("복산"), _ok("백제")
    o, _ = _run({"복산": a, "백제": b}, locks={"복산": None})
    assert a.orders == [] and o.supplier == "백제"


def test_renew_failure_skips_supplier():
    a, b = _ok("복산"), _ok("백제")
    o, calls = _run({"복산": a, "백제": b}, renew_ok={"복산": False})
    assert a.orders == [] and o.supplier == "백제"
    assert ("result", "복산", False, "not_sent") in calls


def test_not_sent_refusal_tries_next():
    a = FakeCrawler([sr("복산", "b1", "694003321", "12EA", 30)],
                    OrderResult(success=False, message="장바구니에 다른 품목", reason_code="not_sent"))
    b = _ok("백제")
    o, _ = _run({"복산": a, "백제": b})
    assert o.state == "ordered" and o.supplier == "백제"


def test_confirmed_rejection_tries_next():
    a = FakeCrawler([sr("복산", "b1", "694003321", "12EA", 30)],
                    OrderResult(success=False, message="도매 거부", reason_code="rejected"))
    b = _ok("백제")
    o, calls = _run({"복산": a, "백제": b})
    assert o.supplier == "백제" and ("result", "복산", False, "rejected") in calls


def test_ambiguous_failure_is_unconfirmed():
    a = FakeCrawler([sr("복산", "b1", "694003321", "12EA", 30)],
                    OrderResult(success=False, message="주문 에러: timeout"))
    b = _ok("백제")
    o, calls = _run({"복산": a, "백제": b})
    assert o.state == "unconfirmed" and b.orders == []
    assert ("unconfirmed", "row-복산") in calls                # DB 는 확정 실패가 아니라 미확정
    assert not any(c[0] == "result" and c[1] == "복산" for c in calls)


def test_order_exception_is_unconfirmed():
    a = FakeCrawler([sr("복산", "b1", "694003321", "12EA", 30)], order_raises=True)
    b = _ok("백제")
    o, calls = _run({"복산": a, "백제": b})
    assert o.state == "unconfirmed" and b.orders == [] and ("unconfirmed", "row-복산") in calls


def test_record_failure_after_success_stops():
    a, b = _ok("복산"), _ok("백제")
    o, _ = _run({"복산": a, "백제": b}, result_raises=("복산",))
    assert o.state == "unconfirmed" and o.supplier == "복산"
    assert len(a.orders) == 1 and b.orders == []


def test_pending_failure_sends_nothing_and_tries_next():
    a, b = _ok("복산"), _ok("백제")
    o, _ = _run({"복산": a, "백제": b}, pending_raises=("복산",))
    assert a.orders == [] and o.supplier == "백제"


def test_no_code_or_unit_is_skipped():
    c = _ok("복산")
    out = run_fallback([({"product_name": "나조린", "insurance_code": None, "unit": "10EA"}, 2)], ["복산"],
                       lambda s: c, lambda s: "nolock", lambda s, t: True, lambda s, t: None,
                       lambda *a: "row", lambda *a: None, lambda *a: None)
    assert out[0].state == "skipped" and c.orders == []


def test_no_candidate_fails():
    o, _ = _run({"복산": FakeCrawler([])})
    assert o.state == "failed" and o.supplier is None


def test_cart_action_after_order():
    it = {"quantity": 15}
    assert cart_action_after_order(it, OrderResult(success=True, reason_code="ok")) == ("delete", 0, "")
    act, qty, why = cart_action_after_order(
        it, OrderResult(success=True, reason_code="stock_adjusted", adjusted_quantity=10))
    assert (act, qty) == ("keep_failed", 5) and "10개만" in why
    act, qty, _ = cart_action_after_order(it, OrderResult(success=False, reason_code="stock_zero", message="재고 0"))
    assert (act, qty) == ("fail", 15)
    act, _, why = cart_action_after_order(it, OrderResult(success=False, reason_code="send_unknown"))
    assert act == "hold" and "확인" in why


def test_cart_action_after_fallback():
    def o(state, need, ordered, sup="복산"):
        return FallbackOutcome({"quantity": 15}, need, sup, ordered, state, "m")
    assert cart_action_after_fallback(o("ordered", 5, 5)) == ("delete", 0, "")
    act, qty, why = cart_action_after_fallback(o("ordered", 5, 3))
    assert (act, qty) == ("keep_failed", 2) and "복산" in why
    assert cart_action_after_fallback(o("unconfirmed", 5, 0))[0] == "note"
    assert cart_action_after_fallback(o("failed", 5, 0, None)) == ("none", 0, "")


def test_format_ordered_line():
    full = {"product_name": "정로환", "quantity": 2, "requested_quantity": 2, "price": 4000}
    assert format_ordered_line(full) == "• 정로환 — 2개 — 8,000원"
    part = {"product_name": "베아놀", "quantity": 10, "requested_quantity": 15, "price": 1000}
    line = format_ordered_line(part)
    assert "10개" in line and "요청 15개" in line and "부족 5개" in line and "10,000원" in line

def test_unknown_preserves_confirmed_part():
    c = FakeCrawler([sr('백제', 'b1', '694003321', '12EA', 30)],
                    OrderResult(success=False, reason_code='send_unknown', fulfilled_quantity=3))
    recorded = []
    out = run_fallback([(dict(ITEM), 15)], ['백제'], lambda s: c,
        lambda s: 't', lambda *a: True, lambda *a: None,
        lambda *a: 'row', lambda *a: None,
        lambda row, message, confirmed=None: recorded.append(confirmed))
    assert out[0].ordered_qty == 3 and recorded == [3]


def _two_items_run(acquire_lock, release_lock=lambda s, t: None, outcomes=None):
    a = dict(ITEM, product_name="A")
    b = dict(ITEM, product_name="B")
    out = run_fallback(
        [(a, 5), (b, 5)], ["복산"],
        open_crawler=lambda s: _ok("복산"),
        acquire_lock=acquire_lock, renew_lock=lambda s, t: True, release_lock=release_lock,
        record_pending=lambda it, s, p, q: "row", record_result=lambda *a: None,
        record_unconfirmed=lambda *a, **k: None, outcomes=outcomes)
    return a, b, out


def test_lock_store_error_skips_supplier_without_losing_earlier_outcomes():
    calls = []
    def lock(s):
        calls.append(s)
        if len(calls) == 2:
            raise ConnectionError("redis down")
        return "tok"
    collected = []
    a, b, out = _two_items_run(lock, outcomes=collected)
    assert out is collected
    assert [(o.item["product_name"], o.state) for o in out] == [("A", "ordered"), ("B", "failed")]


def test_release_error_does_not_drop_outcome():
    def release(s, t):
        raise ConnectionError("redis down")
    a, b, out = _two_items_run(lambda s: "tok", release)
    assert [o.state for o in out] == ["ordered", "ordered"]


def test_outcomes_list_keeps_results_when_run_fallback_raises():
    collected = []
    calls = []
    def open_crawler(s):
        calls.append(s)
        if len(calls) == 2:
            raise KeyboardInterrupt  # 예상 밖 예외(Exception 밖)로 run_fallback 이 중단되는 경우
        return _ok("복산")
    a = dict(ITEM, product_name="A"); b = dict(ITEM, product_name="B")
    with pytest.raises(KeyboardInterrupt):
        run_fallback([(a, 5), (b, 5)], ["복산"], open_crawler=open_crawler,
                     acquire_lock=lambda s: "tok", renew_lock=lambda s, t: True, release_lock=lambda s, t: None,
                     record_pending=lambda *x: "row", record_result=lambda *x: None,
                     record_unconfirmed=lambda *x, **k: None, outcomes=collected)
    assert [(o.item["product_name"], o.state) for o in collected] == [("A", "ordered")]
