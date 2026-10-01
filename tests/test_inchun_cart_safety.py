"""인천 — 약국 장바구니 보존 (C1: 단건 수량 조정 재시도, A3: 첫 장바구니 읽기 실패).

실제 InchunCrawler.order / order_batch 와 PartialStockFallbackMixin 을 실행하고
네트워크 경계(_read_cart/_post_order/_add_to_cart/_clear_cart/login)만 메모리로 대신한다.
"""
import pytest

from test_inchun_order_batch import FakeInchun, inchun, sr

PHARM = [("Z1", 1), ("Z2", 2), ("Z3", 3)]


class Fake(FakeInchun):
    def __init__(self, *a, read_fail=0, login_ok=True, **k):
        super().__init__(*a, **k)
        self.read_fail, self.login_ok = read_fail, login_ok
        self.clears, self.logins = 0, 0

    def _read_cart(self):
        if self.read_fail > 0:
            self.read_fail -= 1
            raise inchun.CartReadError("오류 페이지")
        return super()._read_cart()

    def _clear_cart(self):
        self.clears += 1
        super()._clear_cart()

    def login(self, *a):
        self.logins += 1
        return self.login_ok


@pytest.mark.parametrize("real_stock,lose,expect", [
    ({"A": 1}, None, "stock_adjusted"),     # 두 번째 accepted
    ({}, None, "rejected"),                 # 두 번째 rejected
    ({"A": 1}, 2, "send_unknown"),          # 두 번째 unknown
])
def test_c1_adjusted_retry_keeps_pharmacy_cart(real_stock, lose, expect):
    c = Fake(real_stock, {"x": [sr("A", 1)]}, saved=PHARM, lose_response_on=lose)
    r = c.order("A", 2, product_name="x")
    assert c.submits == [[("A", 2)], [("A", 1)]]
    assert r.reason_code == expect
    assert sorted(c.cart) == sorted(PHARM), "약국 장바구니가 사라졌다"


def test_a3_batch_first_read_fails_twice_touches_nothing():
    c = Fake({"A": 9, "B": 9}, {}, saved=PHARM, read_fail=2)
    res = c.order_batch([{"product_id": "A", "quantity": 1}, {"product_id": "B", "quantity": 1}])
    assert c.clears == 0 and c.submits == []
    assert sorted(c.cart) == sorted(PHARM)
    assert [r.reason_code for r in res] == ["not_sent", "not_sent"]
    assert not any(r.success for r in res)


def test_a3_batch_relogin_failure_touches_nothing():
    c = Fake({"A": 9}, {}, saved=PHARM, read_fail=1, login_ok=False)
    res = c.order_batch([{"product_id": "A", "quantity": 1}])
    assert c.logins == 1 and c.clears == 0 and c.submits == []
    assert sorted(c.cart) == sorted(PHARM) and res[0].reason_code == "not_sent"


def test_a3_batch_relogin_then_proceeds():
    c = Fake({"A": 9}, {}, saved=PHARM, read_fail=1)
    res = c.order_batch([{"product_id": "A", "quantity": 1}])
    assert c.logins == 1 and c.submits == [[("A", 1)]]
    assert res[0].success and sorted(c.cart) == sorted(PHARM)


def test_a3_single_first_read_fails_touches_nothing():
    c = Fake({"A": 9}, {}, saved=PHARM, read_fail=2)
    r = c._order_bare("A", 1)
    assert c.clears == 0 and c.submits == []
    assert sorted(c.cart) == sorted(PHARM) and r.reason_code == "not_sent"


def test_a3_single_relogin_then_proceeds():
    c = Fake({"A": 9}, {}, saved=PHARM, read_fail=1)
    r = c._order_bare("A", 1)
    assert c.logins == 1 and r.success and sorted(c.cart) == sorted(PHARM)
