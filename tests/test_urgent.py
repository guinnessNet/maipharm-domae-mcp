import sys
sys.path.insert(0, "src")

import pytest

from domae_mcp.core.crawlers.base import OrderResult, SearchResult
from domae_mcp.cloud.urgent import find_listing, urgent_keywords, urgent_supplier_step


def sr(pid, qty, price=1000, name="x", insurance="INS"):
    return SearchResult(maker="", product_name=name, unit="", insurance_code=insurance,
                        quantity=qty, supplier="s", price=price, product_id=pid)


class Crawler:
    def __init__(self, table, order_result=None, order_raises=False):
        self.table, self.order_result, self.order_raises = table, order_result, order_raises
        self.orders = []

    def search(self, kw):
        v = self.table.get(kw, [])
        if isinstance(v, Exception):
            raise v
        return v

    def order(self, pid, qty, **kw):
        self.orders.append((pid, qty, kw.get("product_name"), kw.get("insurance_code")))
        self.kwargs = kw
        if self.order_raises:
            raise TimeoutError("응답 유실")
        return self.order_result


@pytest.mark.parametrize("name,code,expect_prefix", [
    ("삼아 씨투스건조시럽/100g", "645702221", ["645702221", "씨투스건조시럽", "삼아씨투스건조시럽"]),
    ("`한화 람노스산/100g", "651602421", ["651602421", "람노스산"]),
    ("피알디현탁시럽0.1%(제약사품절)", "645302211", ["645302211", "피알디현탁시럽0.1%"]),
    ("피알디 현탁시럽 0.1%", None, ["피알디", "피알디현탁시럽0.1%"]),
    ("씨투스건조시럽 100g", None, ["씨투스건조시럽"]),
    ("B대웅 베아놀점안액 0.2%/0.3ml/12EA", "12345", ["베아놀점안액"]),
])
def test_urgent_keywords(name, code, expect_prefix):
    assert urgent_keywords(name, code)[:len(expect_prefix)] == expect_prefix


def test_urgent_keywords_empty():
    assert urgent_keywords("", "") == []


def test_urgent_keywords_order_dedup_and_capacity_filter():
    assert urgent_keywords("씨투스/100g", "645702221") == [
        "645702221", "씨투스", "씨투스/100g"
    ]
    assert urgent_keywords("100mg 제품", "123456789") == ["123456789"]


@pytest.mark.parametrize(("name", "code", "expected"), [
    ("`한화 람노스산/100g", "651602421", [
        "651602421", "람노스산", "한화람노스산", "한화 람노스산", "`한화 람노스산/100g"
    ]),
    ("$삼아 씨투스건조시럽/100g", "645702221", [
        "645702221", "씨투스건조시럽", "삼아씨투스건조시럽", "삼아 씨투스건조시럽",
        "$삼아 씨투스건조시럽/100g"
    ]),
    ("B대웅 베아놀점안액 0.2%/0.3ml/12EA", "12345", [
        "베아놀점안액", "대웅베아놀점안액0.2%", "대웅 베아놀점안액 0.2%",
        "B대웅 베아놀점안액 0.2%/0.3ml/12EA"
    ]),
])
def test_keyword_variants_clean_prefix_only_for_head_candidates(name, code, expected):
    assert urgent_keywords(name, code) == expected


@pytest.mark.parametrize("name", ["１００mg 제품", "١٠٠mg 제품"])
def test_unicode_numeric_leading_name_is_omitted(name):
    assert urgent_keywords(name, None) == []


def test_find_listing_skips_failing_keyword():
    c = Crawler({"씨투스건조시럽": [sr("082636", 5)], "645702221": RuntimeError("null")})
    assert find_listing(c, "082636", ["645702221", "씨투스건조시럽"]).product_id == "082636"


def test_find_listing_requires_exact_pid():
    assert find_listing(Crawler({"k": [sr("OTHER", 9)]}), "082636", ["k"]) is None


def _step(c, need=10, reject=None, before_send=None):
    calls = []
    callback = before_send or (lambda: calls.append("claim"))
    s = urgent_supplier_step(c, "P", ["k"], need, before_send=callback, reject_reason=reject)
    return s, calls


def test_skip_without_claim():
    assert _step(Crawler({}))[0].state == "skip"
    assert _step(Crawler({"k": [sr("P", 0)]}))[1] == []
    s, calls = _step(Crawler({"k": [sr("P", 5)]}, OrderResult(success=True)), reject="미확정")
    assert s.state == "skip" and calls == []


def test_filled_uses_adjusted_and_known_product_fields():
    c = Crawler({"k": [sr("P", 9, price=700, name="씨투스", insurance="645702221")]},
                OrderResult(success=True, adjusted_quantity=4, reason_code="stock_adjusted"))
    s, calls = _step(c, need=6)
    assert (s.state, s.qty, s.price) == ("filled", 4, 700)
    assert calls == ["claim"]
    assert c.orders == [("P", 6, "씨투스", "645702221")]


@pytest.mark.parametrize("price", ["?", True, float("nan"), float("inf"), object()])
def test_bad_price_keeps_confirmed_fill(price):
    s, _ = _step(Crawler({"k": [sr("P", 9, price=price)]}, OrderResult(success=True)), need=3)
    assert (s.state, s.qty, s.price, s.fulfilled) == ("filled", 3, 0, 0)


@pytest.mark.parametrize("rc", ["not_sent", "stock_zero"])
def test_confirmed_not_ordered_skips(rc):
    assert _step(Crawler({"k": [sr("P", 5)]}, OrderResult(success=False, reason_code=rc)))[0].state == "skip"


@pytest.mark.parametrize("rc", ["send_unknown", "rejected", "other", None, "isolated_fail", "cart_dirty"])
def test_ambiguous_halts(rc):
    assert _step(Crawler({"k": [sr("P", 5)]}, OrderResult(success=False, reason_code=rc)))[0].state == "halt"


@pytest.mark.parametrize("ful", [1, 3, 5])
def test_positive_partial_fulfillment_is_preserved(ful):
    s, _ = _step(Crawler({"k": [sr("P", 5)]}, OrderResult(success=False, reason_code="send_unknown",
                                                          fulfilled_quantity=ful)), need=5)
    assert s.state == "halt" and s.fulfilled == ful


@pytest.mark.parametrize("rc", ["not_sent", "stock_zero"])
@pytest.mark.parametrize("ful", [0, 1, 3, 5, False, True, None, "0", -1, 6])
def test_safe_skip_requires_validated_integer_zero(rc, ful):
    s, _ = _step(Crawler({"k": [sr("P", 5)]}, OrderResult(success=False, reason_code=rc,
                                                           fulfilled_quantity=ful)), need=5)
    if ful == 0 and type(ful) is int:
        assert s.state == "skip"
    elif type(ful) is int and 0 < ful <= 5:
        assert (s.state, s.fulfilled) == ("halt", ful)
    else:
        assert (s.state, s.fulfilled) == ("halt", 0)


def test_order_exception_halts():
    assert _step(Crawler({"k": [sr("P", 5)]}, order_raises=True))[0].state == "halt"


@pytest.mark.parametrize("bad", [None, "ok", OrderResult(success=True, adjusted_quantity=99),
                                 OrderResult(success=True, adjusted_quantity=0),
                                 OrderResult(success=True, adjusted_quantity=True),
                                 OrderResult(success=True, adjusted_quantity=False),
                                 OrderResult(success=True, adjusted_quantity="2"),
                                 OrderResult(success=True, adjusted_quantity=1.5),
                                 OrderResult(success=True, adjusted_quantity=-1)])
def test_result_interpretation_failure_halts(bad):
    assert _step(Crawler({"k": [sr("P", 5)]}, bad), need=5)[0].state == "halt"


@pytest.mark.parametrize("need,stock", [(True, 5), (False, 5), (1.5, 5), (None, 5), ("bad", 5),
                                        (-1, 5), (5, True), (5, False), (5, 2.5),
                                        (5, None), (5, "bad"), (5, -2)])
def test_malformed_need_or_stock_is_definite_no_send(need, stock):
    c = Crawler({"k": [sr("P", stock)]}, OrderResult(success=True))
    s, calls = _step(c, need=need)
    assert s.state == "skip"
    assert calls == [] and c.orders == []


def test_pre_reject_and_zero_stock_keep_skip_with_malformed_other_quantity():
    rejected, reject_calls = _step(Crawler({"k": [sr("P", "bad")]}, OrderResult(success=True)),
                                   need=None, reject="미확정")
    no_stock, stock_calls = _step(Crawler({"k": [sr("P", 0)]}, OrderResult(success=True)), need="bad")
    assert rejected.state == "skip" and reject_calls == []
    assert no_stock.state == "skip" and stock_calls == []


def test_lossless_integer_string_quantities_are_accepted():
    c = Crawler({"k": [sr("P", "5")]}, OrderResult(success=True))
    s, _ = _step(c, need="3")
    assert (s.state, s.qty, c.orders[0][1]) == ("filled", 3, 3)


def test_claim_failure_propagates_without_order():
    c = Crawler({"k": [sr("P", 5)]}, OrderResult(success=True))

    def boom():
        raise RuntimeError("claim 실패")

    with pytest.raises(RuntimeError):
        urgent_supplier_step(c, "P", ["k"], 5, before_send=boom)
    assert c.orders == []
