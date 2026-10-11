# tests/test_inchun_daily_limit.py
"""인천 일일 주문 한도 — 검색 행 saleqty_N 판독, 한도 초과분 사전 차단, 누적 한도 거부 처리.

서버 레포의 inchun.py 를 경로로 불러온다(INCHUN_PY). 네트워크는 쓰지 않는다.
가짜 사이트 규칙: 품목별 그날 누적 주문량이 한도를 넘으면 묶음 전체를 받지 않는다(장바구니 그대로).
실측 원문은 비공개 서버 레포 픽스처(INCHUN_LIVE_FIXTURES)로만 대조한다.
"""
import json
import os
import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, "src")
sys.path.insert(0, str(Path(__file__).parent))

from test_inchun_order_batch import Crawler, FakeInchun, Resp, Sess, sr  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_limit_memory():
    memory = getattr(Crawler, "_LIMIT_REJECTED", {})
    memory.clear()
    yield
    memory.clear()


def row(i, pid, stock, limit=None, name="가상 시럽/100g", code="000000001"):
    sale = "" if limit is None else f'<input type="hidden" name="saleqty_{i}" id="saleqty_{i}" value="{limit}">'
    return (f'<tr class="ln_physic"><td>{code}</td><td>가상제약</td>'
            f'<td><a href="./PhysicInfo.asp?pc={pid}&amp;ln=0">{name}</a></td><td>100g</td><td></td>'
            f'<td class="td_num">1,000</td><td class="td_num">{stock}</td>'
            f'<td class="qty"><input type="text" name="qty_{i}"><input type="hidden" name="pc_{i}" value="{pid}">'
            f'<input type="hidden" name="stock_{i}" value="{stock}">{sale}</td><td></td><td></td></tr>')


def search_page(rows):
    return f'<div class="wrap_table"><table class="tbl_list"><tbody>{"".join(rows)}</tbody></table></div>'


def real_search_crawler(html):
    c = Crawler.__new__(Crawler)
    c.session, c._vendor_code = Sess([Resp(200, html)]), "V"
    c._login_id = c._login_pw = ""
    c._logged_in = True
    c.ensure_login = lambda *a: True
    return c


class LimitInchun(FakeInchun):
    """FakeInchun + 일일 한도. used = 오늘 이미 주문한 수량."""
    def __init__(self, real_stock, table, limits, used=None, vendor="V", **kw):
        super().__init__(real_stock, table, **kw)
        self.limits, self.used = limits, dict(used or {})
        self._vendor_code = vendor
        self._daily_limits = {}

    def search(self, keyword):
        results = super().search(keyword)
        for r in results:
            self._limit_cache()[r.product_id] = self.limits.get(r.product_id, 0)
        return results

    def _post_order(self, data):
        self.submits.append(list(self.cart))
        over = any(self.limits.get(p, 0) and self.used.get(p, 0) + q > self.limits[p] for p, q in self.cart)
        if not over and all(self.real_stock.get(p, 0) >= q for p, q in self.cart):
            for p, q in self.cart:
                self.used[p] = self.used.get(p, 0) + q
            self.cart = []
        return Resp(200, "")


A = {"product_id": "A", "quantity": 5, "insurance_code": "000000001", "product_name": "가상 시럽/100g"}
B = {"product_id": "B", "quantity": 2, "insurance_code": "000000002", "product_name": "가상 정/30T"}
TABLE = {"000000001": [sr("A", 10)], "000000002": [sr("B", 50)]}


# ── 판독 ──

def test_search_reads_daily_limit_and_keeps_stock():
    c = real_search_crawler(search_page([row(0, "101", 10, 3), row(1, "102", 23, 0), row(2, "103", 7)]))
    res = c.search("가상")
    assert [(r.product_id, r.quantity) for r in res] == [("101", 10), ("102", 23), ("103", 7)]
    assert c._daily_limits == {"101": 3, "102": 0, "103": 0}


@pytest.mark.parametrize("raw", ["", "abc", "-2"])
def test_unreadable_limit_means_no_limit(raw):
    c = real_search_crawler(search_page([row(0, "101", 10).replace(
        "</td><td></td><td></td></tr>", f'<input type="hidden" name="saleqty_0" value="{raw}"></td><td></td><td></td></tr>')]))
    c.search("가상")
    assert c._daily_limits == {"101": 0}


def test_refetch_returns_smaller_of_stock_and_limit():
    c = LimitInchun({"A": 10}, TABLE, {"A": 3})
    assert c._refetch_stock_for_item(A) == 3
    c = LimitInchun({"A": 2}, {"000000001": [sr("A", 2)]}, {"A": 3})
    assert c._refetch_stock_for_item(A) == 2


# ── 단건 주문 ──

def test_single_over_limit_sends_only_limit():
    c = LimitInchun({"A": 10}, TABLE, {"A": 3})
    r = c.order("A", 5, product_name=A["product_name"], insurance_code=A["insurance_code"])
    assert c.submits == [[("A", 3)]]
    assert r.success and r.reason_code == "stock_adjusted" and r.adjusted_quantity == 3
    assert r.message == "일일 주문 한도 3개로 5→3개 조정 주문"


def test_single_within_limit_is_plain_ok():
    c = LimitInchun({"A": 10}, TABLE, {"A": 3})
    r = c.order("A", 3, product_name=A["product_name"], insurance_code=A["insurance_code"])
    assert c.submits == [[("A", 3)]] and r.success and r.reason_code == "ok"


def test_single_stock_below_limit_stays_stock_shortage():
    c = LimitInchun({"A": 2}, {"000000001": [sr("A", 2)]}, {"A": 3})
    r = c.order("A", 5, product_name=A["product_name"], insurance_code=A["insurance_code"])
    assert c.submits == [[("A", 2)]]     # 한도 초과라 첫 시도는 보내지 않고, 재고(2)로 줄여 보낸다
    assert r.success and r.adjusted_quantity == 2 and r.message.startswith("재고 부족")


def test_single_cumulative_limit_is_rejected_then_not_resent_same_day():
    """2026-10-11 재현: 12:59 에 3개 주문(한도 3) → 14:01 3개 재전송 → 도매가 받지 않음."""
    c = LimitInchun({"A": 10}, TABLE, {"A": 3}, used={"A": 3})
    r = c.order("A", 3, product_name=A["product_name"], insurance_code=A["insurance_code"])
    assert c.submits == [[("A", 3)]]
    assert r.success is False and r.reason_code == "rejected"
    assert "일일 주문 한도 3개" in r.message

    c2 = LimitInchun({"A": 10}, TABLE, {"A": 3}, used={"A": 3})   # 다음 회차 — 새 인스턴스
    r2 = c2.order("A", 3, product_name=A["product_name"], insurance_code=A["insurance_code"])
    assert c2.submits == []
    assert r2.success is False and r2.reason_code == "not_sent" and "오늘 이미" in r2.message


def test_limit_rejection_memory_is_per_day_and_vendor(monkeypatch):
    c = LimitInchun({"A": 10}, TABLE, {"A": 3}, used={"A": 3})
    c.order("A", 3, product_name=A["product_name"], insurance_code=A["insurance_code"])
    other = LimitInchun({"A": 10}, TABLE, {"A": 3}, vendor="W")      # 다른 약국
    assert other.order("A", 3, product_name=A["product_name"], insurance_code=A["insurance_code"]).success
    monkeypatch.setattr(Crawler, "_today_kst", staticmethod(lambda: date(2099, 1, 1)))
    nxt = LimitInchun({"A": 10}, TABLE, {"A": 3})                     # 다음 날
    assert nxt.order("A", 3, product_name=A["product_name"], insurance_code=A["insurance_code"]).success


def test_no_limit_behaves_as_before():
    c = LimitInchun({"A": 10}, TABLE, {})
    r = c.order("A", 5, product_name=A["product_name"], insurance_code=A["insurance_code"])
    assert c.submits == [[("A", 5)]] and r.success and r.reason_code == "ok"


# ── 일괄 주문 ──

def test_batch_over_limit_skips_phase1_and_caps():
    c = LimitInchun({"A": 10, "B": 50}, TABLE, {"A": 3})
    res = c.order_batch([dict(A), dict(B)])
    assert c.submits == [[("A", 3), ("B", 2)]]
    assert res[0].success and res[0].adjusted_quantity == 3 and "일일 주문 한도 3개" in res[0].message
    assert res[1].success and res[1].reason_code == "ok"


def test_batch_cumulative_limit_isolates_limited_item():
    c = LimitInchun({"A": 10, "B": 50}, TABLE, {"A": 3}, used={"A": 3})
    res = c.order_batch([dict(A, quantity=3), dict(B)])
    assert c.submits[-2:] == [[("B", 2)], [("A", 3)]]
    assert res[1].success
    assert res[0].success is False and res[0].reason_code == "rejected" and "일일 주문 한도" in res[0].message


def test_batch_single_limited_item_is_not_split_again():
    c = LimitInchun({"A": 10}, TABLE, {"A": 3}, used={"A": 3})
    res = c.order_batch([dict(A, quantity=3)])
    assert c.submits == [[("A", 3)], [("A", 3)]]
    assert res[0].reason_code == "rejected"


def test_batch_restores_pharmacy_cart_after_limit_handling():
    c = LimitInchun({"A": 10, "B": 50}, TABLE, {"A": 3}, saved=[("Z", 4)])
    c.order_batch([dict(A), dict(B)])
    assert c.cart == [("Z", 4)]


def test_batch_known_limit_survives_failed_restock_search():
    """검수 지적 1: 한도(3)를 이미 아는데 Phase 2 재검색이 실패해도 한도를 넘겨 보내지 않는다."""
    c = LimitInchun({"A": 10, "B": 50}, {}, {"A": 3})
    c._daily_limits = {"A": 3, "B": 0}
    res = c.order_batch([dict(A), dict(B)])
    assert c.submits == [[("A", 3), ("B", 2)]]
    assert res[0].success and res[0].adjusted_quantity == 3 and "일일 주문 한도 3개" in res[0].message
    assert res[1].success


class FlakyLimitInchun(LimitInchun):
    """첫 번째 보험코드 검색만 실패(빈 결과)한다."""
    def search(self, keyword):
        if keyword == "000000001" and "flaked" not in self.__dict__:
            self.flaked = True
            self.queries.append(keyword)
            return []
        return super().search(keyword)


def test_batch_limit_learned_in_phase2_still_isolates():
    """검수 지적 2: 주문 직전 한도 확인은 실패했고 Phase 2 에서 처음 알게 된 한도도 분리에 쓴다."""
    c = FlakyLimitInchun({"A": 10, "B": 50}, TABLE, {"A": 3}, used={"A": 3})
    res = c.order_batch([dict(A, quantity=3), dict(B)])
    assert c.submits[-2:] == [[("B", 2)], [("A", 3)]]
    assert res[1].success
    assert res[0].reason_code == "rejected" and "일일 주문 한도 3개" in res[0].message


# ── 실측 원문 (비공개 픽스처) ──

LIVE = os.environ.get("INCHUN_LIVE_FIXTURES")


@pytest.mark.skipif(not LIVE, reason="INCHUN_LIVE_FIXTURES 미지정")
def test_live_search_rows_daily_limit():
    base = Path(LIVE)
    expected = json.loads((base / "expected.json").read_text(encoding="utf-8"))
    c = real_search_crawler((base / "search_citus_rows.html").read_text(encoding="utf-8"))
    res = {r.product_id: r.quantity for r in c.search("가상")}
    for pid, v in expected["rows"].items():
        assert res[pid] == v["stock"] and c._daily_limits[pid] == v["limit"]
    assert c._daily_limits[expected["roles"]["limited"]] > 0
    assert c._daily_limits[expected["roles"]["unlimited"]] == 0
