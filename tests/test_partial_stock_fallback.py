# tests/test_partial_stock_fallback.py
"""PartialStockFallbackMixin — 재고조회 훅, Phase 3(unknown 분리 재전송), 단건 래퍼 테스트.

FakeSite 는 인천처럼 동작한다: 장바구니에 재고를 넘는 품목이 하나라도 있으면 전체를 거부한다.
"""
import sys
sys.path.insert(0, "src")

from domae_mcp.core.crawlers.base import BaseCrawler, OrderResult, PartialStockFallbackMixin


class FakeSite(PartialStockFallbackMixin, BaseCrawler):
    SUPPLIER_NAME = "fake"

    def login(self, *a):
        return True

    def search(self, keyword):
        return []

    def __init__(self, real_stock, visible_stock):
        self.real_stock = real_stock          # 실제 재고 (전송 판정용)
        self.visible_stock = visible_stock    # refetch 로 보이는 재고 (None = 조회 실패)
        self.cart = []
        self.submits = []                     # 전송된 장바구니 스냅샷
        self.clear_raises = False
        self.submit_raises_on = None          # n번째 submit 에서 예외 (1부터)

    def refetch_stock(self, product_id, product_name=""):
        return self.visible_stock.get(product_id)

    def add(self, pid, qty):
        self.cart.append((pid, qty))

    def clear(self):
        if self.clear_raises:
            raise RuntimeError("장바구니 비우기 실패")
        self.cart = []

    def submit(self, expected=None):
        self.submits.append(list(self.cart))
        if self.submit_raises_on == len(self.submits):
            raise TimeoutError("응답 없음")
        if all(self.real_stock.get(pid, 0) >= qty for pid, qty in self.cart):
            self.cart = []
            return "accepted"
        return "rejected"


def _items():
    return [
        {"product_id": "A", "quantity": 2, "product_name": "정로환"},     # 재고 확인됨, 충분
        {"product_id": "B", "quantity": 15, "product_name": "베아놀"},    # 조회 실패, 실제 품절
        {"product_id": "C", "quantity": 1, "product_name": "이가탄"},     # 조회 실패, 실제 충분
    ]


def _site():
    return FakeSite(real_stock={"A": 10, "B": 0, "C": 5},
                    visible_stock={"A": 10, "B": None, "C": None})


def _phase2(site, items):
    plans = site._compute_adjusted_plan(items)
    site.clear()
    for p in plans:
        if p["submit_qty"] > 0:
            site.add(p["item"]["product_id"], p["submit_qty"])
    return plans, site.submit() == "accepted"


def test_refetch_hook_defaults_to_refetch_stock():
    site = FakeSite(real_stock={}, visible_stock={"A": 7})
    assert site._refetch_stock_for_item({"product_id": "A", "product_name": "x"}) == 7


def test_unknown_stockout_is_isolated():
    site = _site()
    plans, ok = _phase2(site, _items())
    assert ok is False                                   # Phase 2 는 B 때문에 전체 거부

    site._isolate_unknown_and_resubmit(plans, site.add, site.submit, site.clear)
    results = site._build_results_from_plan(plans, ok)

    assert [r.success for r in results] == [True, False, True]
    assert results[1].reason_code == "isolated_fail"
    assert site.submits[1:] == [[("A", 2)], [("B", 15)], [("C", 1)]]


def test_no_unknown_means_no_extra_submit():
    site = FakeSite(real_stock={"A": 0}, visible_stock={"A": 10})   # 보이는 재고와 실제가 다름
    plans, ok = _phase2(site, [{"product_id": "A", "quantity": 2, "product_name": "x"}])
    before = len(site.submits)
    site._isolate_unknown_and_resubmit(plans, site.add, site.submit, site.clear)
    assert len(site.submits) == before


def test_clear_failure_sends_nothing():
    site = _site()
    plans, ok = _phase2(site, _items())
    site.clear_raises = True
    before = len(site.submits)
    site._isolate_unknown_and_resubmit(plans, site.add, site.submit, site.clear)
    assert len(site.submits) == before                   # 비우기 실패 시 전송 금지
    results = site._build_results_from_plan(plans, ok)
    assert [r.reason_code for r in results] == ["not_sent", "not_sent", "not_sent"]


def test_submit_exception_stops_phase3():
    site = _site()
    plans, ok = _phase2(site, _items())                  # submit #1 (Phase 2)
    site.submit_raises_on = 2                            # Phase 3 첫 묶음(A)에서 응답 없음
    site._isolate_unknown_and_resubmit(plans, site.add, site.submit, site.clear)
    results = site._build_results_from_plan(plans, ok)
    assert len(site.submits) == 2                        # 결과 불명 이후 더 보내지 않는다
    assert results[0].reason_code == "send_unknown"
    assert results[1].reason_code == results[2].reason_code == "not_sent"


def test_bool_false_submit_is_unknown():
    """bool 만 주는 전송 함수의 False 는 거부인지 결과 불명인지 모른다 → unknown 으로 멈춘다."""
    site = _site()
    plans, ok = _phase2(site, _items())
    site._isolate_unknown_and_resubmit(plans, site.add, lambda expected: False, site.clear)
    results = site._build_results_from_plan(plans, ok)
    assert results[0].reason_code == "send_unknown"
    assert results[1].reason_code == results[2].reason_code == "not_sent"


def test_single_order_unknown_not_retried():
    site = FakeSite(real_stock={"A": 1}, visible_stock={"A": 1})
    calls = []

    def bare(pid, qty, **metadata):
        calls.append(qty)
        return OrderResult(success=False, message="결과 불명", reason_code="send_unknown")

    r = site._order_with_stock_fallback(bare, "A", 5, product_name="x")
    assert calls == [5] and r.reason_code == "send_unknown"    # 재고 1 이어도 재시도하지 않는다


def _scripted(*results):
    """호출될 때마다 준비된 OrderResult 를 차례로 돌려주는 bare 주문 함수."""
    calls = []

    def bare(pid, qty, **metadata):
        calls.append(qty)
        return results[len(calls) - 1]
    return bare, calls


def test_adjusted_resend_unknown_is_preserved():
    """4차 검수 재현: 5개 거부 → 2개로 조정 전송 → 응답 유실. 최종 사유는 send_unknown 이어야 한다."""
    site = FakeSite(real_stock={"A": 2}, visible_stock={"A": 2})
    bare, calls = _scripted(OrderResult(success=False, message="거부", reason_code="rejected"),
                            OrderResult(success=False, message="결과 불명", reason_code="send_unknown"))
    r = site._order_with_stock_fallback(bare, "A", 5, product_name="x")
    assert calls == [5, 2]
    assert r.reason_code == "send_unknown" and r.success is False


def test_wrapper_keeps_confirmed_reasons():
    site = FakeSite(real_stock={}, visible_stock={"A": None})
    bare, _ = _scripted(OrderResult(success=False, message="전송 안 함", reason_code="not_sent"))
    assert site._order_with_stock_fallback(bare, "A", 5).reason_code == "not_sent"


class FakeBase(BaseCrawler):
    """order_batch 기본 구현(BaseCrawler)을 쓰는 크롤러."""
    SUPPLIER_NAME = "base"

    def __init__(self, results, stock):
        self._results, self._stock, self.calls = list(results), stock, []

    def login(self, *a):
        return True

    def search(self, keyword):
        return []

    def refetch_stock(self, product_id, product_name=""):
        return self._stock

    def order(self, product_id, quantity, **kw):
        self.calls.append(quantity)
        return self._results.pop(0)


def test_default_order_batch_does_not_resend_unknown():
    c = FakeBase([OrderResult(success=False, message="결과 불명", reason_code="send_unknown")], stock=2)
    (r,) = c.order_batch([{"product_id": "A", "quantity": 5}])
    assert c.calls == [5] and r.reason_code == "send_unknown"


def test_default_order_batch_adjusted_unknown_preserved():
    c = FakeBase([OrderResult(success=False, message="거부", reason_code="rejected"),
                  OrderResult(success=False, message="결과 불명", reason_code="send_unknown")], stock=2)
    (r,) = c.order_batch([{"product_id": "A", "quantity": 5}])
    assert c.calls == [5, 2] and r.reason_code == "send_unknown"


def test_single_adjustment_clear_failure_blocks_resend():
    site = FakeSite(real_stock={"A": 2}, visible_stock={"A": 2})
    site._clear_cart = site.clear
    site.clear_raises = True
    bare, calls = _scripted(OrderResult(success=False, reason_code="rejected"),
                            OrderResult(success=True))
    result = site._order_with_stock_fallback(bare, "A", 5)
    assert calls == [5]
    assert result.reason_code == "not_sent"


def test_unclassified_reason_defaults_to_other():
    from domae_mcp.core.crawlers.base import _keep_or_other
    assert _keep_or_other(None) == "other"
    assert _keep_or_other("legacy") == "other"


def test_final_rejected_overrides_previous_success():
    site = FakeSite(real_stock={}, visible_stock={"A": 2})
    plan = site._compute_adjusted_plan([{"product_id": "A", "quantity": 1}])
    plan[0]["final"] = "rejected"
    result = site._build_results_from_plan(plan, True)[0]
    assert result.success is False and result.reason_code == "rejected"
