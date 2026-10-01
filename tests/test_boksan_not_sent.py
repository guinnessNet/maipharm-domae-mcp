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
