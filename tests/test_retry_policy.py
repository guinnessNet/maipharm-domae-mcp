"""Only confirmed unsent orders may be reduced and resent."""
import sys
sys.path.insert(0, "src")

import pytest
from domae_mcp.core.crawlers import base
from domae_mcp.core.crawlers.base import BaseCrawler, PartialStockFallbackMixin, OrderResult, SearchResult
from domae_mcp.cloud.scheduler import _is_item_retryable


class Crawler(PartialStockFallbackMixin, BaseCrawler):
    def __init__(self, results, stock=2):
        super().__init__()
        self.results = iter(results)
        self.stock = stock
        self.calls = []
        self.refetches = []
    def login(self, *args): return True
    def search(self, keyword): return []
    def _refetch_stock_for_item(self, item):
        self.refetches.append(item)
        return self.stock
    def bare(self, pid, qty, **metadata):
        self.calls.append((pid, qty, metadata))
        return next(self.results)
    def order(self, pid, qty, **metadata):
        return self.bare(pid, qty, **metadata)


def execute(crawler, batch=False, **metadata):
    if batch:
        return crawler.order_batch([dict(product_id="p", quantity=5, **metadata)])[0]
    return crawler._order_with_stock_fallback(crawler.bare, "p", 5, **metadata)


@pytest.mark.parametrize("batch", [False, True])
@pytest.mark.parametrize("reason", [None, "other", "send_unknown", "isolated_fail", "cart_dirty"])
def test_uncertain_failure_never_refetches_or_resends(batch, reason):
    r = OrderResult(reason_code=reason)
    c = Crawler([r], stock=0)
    assert execute(c, batch) is r
    assert len(c.calls) == 1 and c.refetches == []
    assert r.reason_code == ("send_unknown" if reason in (None, "other") else reason)


@pytest.mark.parametrize("batch", [False, True])
@pytest.mark.parametrize("actual", [None, 1])
def test_safe_resend_retains_actual_partial_quantity(batch, actual):
    c = Crawler([OrderResult(reason_code="not_sent"), OrderResult(success=True, adjusted_quantity=actual)])
    r = execute(c, batch, product_name="약 이름", insurance_code="123456789")
    assert [x[1] for x in c.calls] == [5, 2]
    assert r.adjusted_quantity == (2 if actual is None else actual)
    assert r.reason_code == "stock_adjusted" and r.retried
    assert f"5→{r.adjusted_quantity}" in r.message
    assert all(x[2] == {"product_name": "약 이름", "insurance_code": "123456789"} for x in c.calls)


@pytest.mark.parametrize("batch", [False, True])
@pytest.mark.parametrize("actual", [0, 9, True, 1.5, "1", -1])
def test_invalid_resend_quantity_is_unknown(batch, actual, caplog):
    c = Crawler([OrderResult(reason_code="not_sent"), OrderResult(success=True, adjusted_quantity=actual)])
    r = execute(c, batch)
    assert not r.success and r.reason_code == "send_unknown"
    assert caplog.records


@pytest.mark.parametrize("batch", [False, True])
@pytest.mark.parametrize("blocked", ["partial", "no_retry"])
def test_partial_or_no_retry_result_is_untouched(batch, blocked):
    r = OrderResult(reason_code="not_sent", fulfilled_quantity=3 if blocked == "partial" else 0)
    if blocked == "no_retry": r.no_retry = True
    c = Crawler([r])
    assert execute(c, batch) is r
    assert len(c.calls) == 1 and not c.refetches
    assert r.fulfilled_quantity == (3 if blocked == "partial" else 0)


@pytest.mark.parametrize("reason", [None, "other", "cart_dirty", "rejected"])
def test_second_failure_preserves_or_normalizes_reason(reason):
    c = Crawler([OrderResult(reason_code="not_sent"), OrderResult(reason_code=reason, adjusted_quantity=1)])
    r = execute(c)
    assert r.reason_code == ("send_unknown" if reason in (None, "other") else reason)
    assert r.adjusted_quantity == 1


@pytest.mark.parametrize("value,expected", [(0,0),(3,3),(5,5),(True,None),(1.0,None),("1",None),(-1,None),(6,None)])
def test_checked_qty(value, expected):
    assert base.checked_qty(value, 5) == expected


def test_presend_candidates_advance_after_exception_and_require_exact_pid():
    c = Crawler([])
    calls = []
    def search(keyword):
        calls.append(keyword)
        if keyword == "123456789": raise RuntimeError("search unavailable")
        return [SearchResult(product_id="other", quantity=999), SearchResult(product_id="p", quantity=2)]
    c.search = search
    assert c.presend_stock(dict(product_id="p", product_name=" 약 이름 ", insurance_code="123456789")) == 2
    assert calls == ["123456789", "약이름"]


def test_presend_multicenter_missing_and_dedup():
    c = Crawler([])
    calls = []
    c.search = lambda keyword: calls.append(keyword) or []
    assert c.presend_stock(dict(product_id="p", product_name=" p ", insurance_code="invalid")) is None
    assert calls == ["p"]
    c.search = lambda keyword: [SearchResult(product_id="p", quantity=99, local_stock=2, other_stock=3)]
    assert c.presend_stock(dict(product_id="p")) == 5
    c.search = lambda keyword: [SearchResult(product_id="p", quantity=-4)]
    assert c.presend_stock(dict(product_id="p")) == 0


@pytest.mark.parametrize("reason", ["not_sent", "rejected", None, "other", "send_unknown", "stock_zero", "isolated_fail", "cart_dirty"])
def test_scheduler_retry_requires_safe_reason(reason):
    r = OrderResult(reason_code=reason, message="세션 만료")
    assert _is_item_retryable(r) == (reason in ("not_sent", "rejected"))
    r.fulfilled_quantity = 3
    assert not _is_item_retryable(r)
    r.fulfilled_quantity = 0
    r.no_retry = True
    assert not _is_item_retryable(r)


def test_default_guard_and_no_retry():
    assert Crawler([]).send_guard is None
    assert OrderResult().no_retry is False


def test_send_guard_default_survives_legacy_initializer():
    class LegacyCrawler(Crawler):
        def __init__(self): pass
    assert LegacyCrawler().send_guard is None


def test_invalid_resend_quantity_has_confirmation_message():
    c = Crawler([OrderResult(reason_code="not_sent"), OrderResult(success=True, adjusted_quantity=9)])
    r = execute(c)
    assert "주문내역 확인" in r.message


@pytest.mark.parametrize("batch", [False, True])
@pytest.mark.parametrize("reason", ["not_sent", "rejected"])
@pytest.mark.parametrize("fulfilled", [False, 0.0, None, "0", -1, 6])
def test_invalid_initial_fulfillment_never_authorizes_base_resend(batch, reason, fulfilled):
    r = OrderResult(reason_code=reason, fulfilled_quantity=fulfilled)
    c = Crawler([r, OrderResult(success=True)])
    try:
        assert execute(c, batch) is r
        assert len(c.calls) == 1 and not c.refetches
        assert r.reason_code == "send_unknown" and not r.success
        assert r.fulfilled_quantity is fulfilled
        assert base.confirmed_quantity(r, 5) is None
    finally:
        c.session.close()


@pytest.mark.parametrize("fulfilled", [False, 0.0, None, "0", -1, 6])
def test_retry_predicate_rejects_unvalidated_zero(fulfilled):
    assert not _is_item_retryable(OrderResult(reason_code="not_sent", fulfilled_quantity=fulfilled))


@pytest.mark.parametrize("batch", [False, True])
@pytest.mark.parametrize("reason", ["not_sent", "rejected"])
def test_valid_integer_zero_still_authorizes_base_resend(batch, reason):
    c = Crawler([OrderResult(reason_code=reason, fulfilled_quantity=0), OrderResult(success=True)])
    try:
        assert execute(c, batch).success
        assert [x[1] for x in c.calls] == [5, 2]
    finally:
        c.session.close()
