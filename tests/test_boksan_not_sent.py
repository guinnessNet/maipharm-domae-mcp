# tests/test_boksan_not_sent.py
import importlib.util
import os
import sys
sys.path.insert(0, "src")

PATH = os.environ.get(
    "BOKSAN_PY",
    os.path.expanduser("~/Desktop/pharmsquare-server-main/prisma/seeds/domae-crawlers/boksan.py"),
)
spec = importlib.util.spec_from_file_location("boksan_under_test", PATH)
boksan = importlib.util.module_from_spec(spec)
spec.loader.exec_module(boksan)


class Fake(boksan.BoksanCrawler):
    def __init__(self, cart):
        self.cart = cart
        self.submitted = False
        self._login_id = self._login_pw = ""
        self._logged_in = True

    def ensure_login(self, *a):
        return True

    def _get_cart_items(self):
        return self.cart

    def _submit_order_safe(self):
        self.submitted = True
        return True, "ok"


def test_foreign_item_refusal_is_not_sent():
    c = Fake([{"pc": "OTHER", "qty": "1", "price": "100"}])
    r = c.order("TARGET", 1)
    assert r.success is False and r.reason_code == "not_sent" and not c.submitted


def test_qty_mismatch_refusal_is_not_sent():
    c = Fake([{"pc": "TARGET", "qty": "5", "price": "100"}])
    r = c.order("TARGET", 1)
    assert r.reason_code == "not_sent" and not c.submitted


def test_submit_path_unchanged():
    c = Fake([{"pc": "TARGET", "qty": "1", "price": "100"}])
    r = c.order("TARGET", 1)
    assert r.success is True and c.submitted


# ── A2: 이번 호출이 새로 담은 품목이 검증에 걸리면 not_sent 로 넘기지 않는다 ──
# 복산에는 한 줄 삭제 API 가 없다(remove_from_cart 는 전체 비우기 후 재담기 = 약국 장바구니 변경).
# 그래서 지우지 않고 cart_dirty 로 반환해 대체주문이 다음 순번으로 넘어가지 않게 한다.

class AddFake(Fake):
    """빈 장바구니 → 담으면 `added_line` 이 담긴다 (검증 실패를 일으키도록 조작 가능)."""
    def __init__(self, added_line, cart=None):
        super().__init__(list(cart or []))
        self.added_line, self.adds, self.removes, self.clears = added_line, [], 0, 0

    def _resolve_row(self, pid):
        return None

    def _add_to_cart(self, pid, qty, stock=None, price=0):
        self.adds.append((pid, qty))
        self.cart.append(dict(self.added_line))

    def remove_from_cart(self, pid):
        self.removes += 1

    def _clear_cart(self):
        self.clears += 1


import pytest  # noqa: E402


@pytest.mark.parametrize("line", [
    {"pc": "TARGET", "qty": "3", "price": "100"},    # 수량 불일치
    {"pc": "TARGET", "qty": "1", "price": "0"},      # 단가 0
    {"pc": "OTHER", "qty": "1", "price": "100"},     # 다른 품목
])
def test_new_add_then_validation_failure_is_cart_dirty(line):
    c = AddFake(line)
    r = c.order("TARGET", 1, price=100)
    assert c.adds == [("TARGET", 1)] and not c.submitted
    assert r.success is False and r.reason_code == "cart_dirty"
    assert c.removes == 0 and c.clears == 0, "약국 장바구니를 건드리는 삭제를 하면 안 된다"


def test_existing_item_validation_failure_stays_not_sent_without_delete():
    c = AddFake({"pc": "X", "qty": "1", "price": "1"}, cart=[{"pc": "TARGET", "qty": "5", "price": "100"}])
    r = c.order("TARGET", 1)
    assert r.reason_code == "not_sent" and c.adds == [] and c.removes == 0 and c.clears == 0


def test_new_add_then_read_error_is_cart_dirty():
    class Boom(AddFake):
        def _get_cart_items(self):
            if self.adds:
                raise RuntimeError("조회 실패")
            return self.cart
    c = Boom({"pc": "TARGET", "qty": "1", "price": "100"})
    r = c.order("TARGET", 1, price=100)
    assert r.reason_code == "cart_dirty" and not c.submitted


def test_cart_dirty_is_not_retried_or_continued():
    from domae_mcp.cloud import scheduler as sch
    from domae_mcp.cloud import fallback as fb
    from domae_mcp.core.crawlers.base import OrderResult
    r = OrderResult(success=False, message="x", reason_code="cart_dirty")
    assert sch._is_item_retryable(r) is False
    assert "cart_dirty" not in fb.SAFE_TO_CONTINUE
