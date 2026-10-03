"""텔레그램 주문 결과 문구 — 실제 주문 수량, 재시도 표시, 목록 잘림."""
import sys
sys.path.insert(0, "src")

from domae_mcp.core.crawlers.base import BaseCrawler, OrderResult, PartialStockFallbackMixin
from domae_mcp.cloud import scheduler as sch
from domae_mcp.cloud.fallback import format_ordered_line


# ── 재시도 표시: 크롤러 공통 경로가 retried 를 세운다 ──────────────────────

class Site(PartialStockFallbackMixin, BaseCrawler):
    SUPPLIER_NAME = "fake"

    def __init__(self):
        pass

    def login(self, *a):
        return True

    def search(self, keyword):
        return []

    def refetch_stock(self, product_id, product_name=""):
        return 2


def test_order_result_retried_defaults_false():
    assert OrderResult(success=True).retried is False


def test_phase2_success_results_are_marked_retried():
    plans = [{"item": {"product_id": "A", "quantity": 2}, "submit_qty": 2, "reason_code": "ok",
              "available_stock": 9},
             {"item": {"product_id": "B", "quantity": 5}, "submit_qty": 2, "reason_code": "stock_adjusted",
              "available_stock": 2},
             {"item": {"product_id": "C", "quantity": 1}, "submit_qty": 0, "reason_code": "stock_zero",
              "available_stock": 0}]
    res = Site()._build_results_from_plan(plans, True)
    assert [r.retried for r in res] == [True, True, False]


def test_adjusted_resend_is_marked_retried():
    calls = []

    def bare(pid, qty, **metadata):
        calls.append(qty)
        if len(calls) == 1:
            return OrderResult(success=False, message="거부", reason_code="rejected")
        return OrderResult(success=True, message="주문 전송 완료")

    r = Site()._order_with_stock_fallback(bare, "A", 5, product_name="x")
    assert r.success and r.retried is True and r.adjusted_quantity == 2


def test_first_try_success_is_not_retried():
    r = Site()._order_with_stock_fallback(lambda pid, qty, **metadata: OrderResult(success=True), "A", 5)
    assert r.retried is False


# ── 알림 문구 ─────────────────────────────────────────────────────────────

def test_ordered_line_shows_retry():
    line = format_ordered_line({"product_name": "정로환", "quantity": 2, "requested_quantity": 2,
                                "price": 4000, "retried": True})
    assert line.endswith("(재시도 후 주문)")
    assert "재시도" not in format_ordered_line({"product_name": "정로환", "quantity": 2, "price": 4000})


def test_quick_order_message_uses_actual_quantity():
    r = OrderResult(success=True, message="재고 부족으로 5→2개 조정 주문", adjusted_quantity=2,
                    reason_code="stock_adjusted", retried=True)
    msg = sch._quick_order_message("인천", "베아놀", 5, r, True)
    assert "2개 주문 완료" in msg and "요청 5개" in msg and "5개 주문 완료" not in msg
    assert "재시도 후 주문" in msg


def test_quick_order_message_other_states():
    assert sch._quick_order_message("인천", "A", 3, OrderResult(success=True), True) == "✅ [인천] A 3개 주문 완료"
    assert "확인 필요" in sch._quick_order_message(
        "인천", "A", 3, OrderResult(success=False, reason_code="send_unknown"), None)
    assert sch._quick_order_message(
        "인천", "A", 3, OrderResult(success=False, message="재고 0"), False).startswith("❌ [인천] A 주문 실패")


def test_batch_success_line_marks_retry():
    item = {"product_name": "정로환", "quantity": 2}
    assert sch._batch_success_line("인천", item, OrderResult(success=True)) == " · [인천] 정로환 ×2"
    assert sch._batch_success_line("인천", item, OrderResult(success=True, retried=True)).endswith("(재시도 후 주문)")


def test_auto_order_telegram_lists_overflow(monkeypatch):
    sent = []
    import domae_mcp.cloud.notifier as nmod
    monkeypatch.setattr(nmod.Notifier, "send_telegram",
                        staticmethod(lambda chat_id, text, reply_markup=None: sent.append(text)))
    s = sch.CloudScheduler(None, None)
    items = [{"product_name": f"약{i}", "quantity": 1, "requested_quantity": 1, "price": 100} for i in range(12)]
    s._send_auto_order_telegram("1", "인천", items, [])
    assert "외 2건" in sent[0] and "총 12건" in sent[0]
