# tests/test_inchun_order_batch.py
"""인천 크롤러 — 장바구니 판독, 전송 전 대조, 전송 결과 판정, order_batch() 전체 흐름, 재고 재조회.

서버 레포의 inchun.py 를 경로로 불러온다(INCHUN_PY 로 변경 가능). 네트워크는 쓰지 않는다.
가짜 객체는 네트워크 경계(session.get/post 또는 _read_cart/_post_order)만 대신하고,
대조·판정 로직은 실제 코드가 실행된다.
"""
import importlib.util
import os
import sys
sys.path.insert(0, "src")

from bs4 import BeautifulSoup as bs

from domae_mcp.core.crawlers.base import SearchResult

PATH = os.environ.get(
    "INCHUN_PY",
    os.path.expanduser("~/Desktop/pharmsquare-server-main/prisma/seeds/domae-crawlers/inchun.py"),
)
spec = importlib.util.spec_from_file_location("inchun_under_test", PATH)
inchun = importlib.util.module_from_spec(spec)
spec.loader.exec_module(inchun)
Crawler = inchun.InchunCrawler


def sr(pid, qty, name="x"):
    return SearchResult(maker="", product_name=name, unit="", insurance_code="",
                        quantity=qty, supplier="인천", price=1000, product_id=pid)


def page(lines):
    """정상 장바구니 페이지 (frmBag 폼 + intArray). lines = [(pc, qty, price)]"""
    rows = "".join(
        f'<input name="pc_{i}" value="{pc}"><input name="bagQty_{i}" value="{q}">'
        f'<input name="price_{i}" value="{p}"><input name="stock_{i}" value="0">'
        for i, (pc, q, p) in enumerate(lines))
    return f'<form name="frmBag" action="./OrderEnd.asp"><input name="intArray" value="">{rows}</form>'


ERROR_PAGE = "<html><script>alert('세션이 만료되었습니다');</script></html>"   # 실측 88자 비로그인 응답과 같은 부류


# ── 1) 실제 _read_cart / _submit_order_status 를 가짜 세션으로 검증 ─────────

class Resp:
    def __init__(self, status=200, text="", url="https://inchunpharm.com/Service/Order/Bag.asp"):
        self.status_code, self.text, self.url = status, text, url


class Sess:
    """GET 은 준비된 페이지를 순서대로 돌려준다."""
    def __init__(self, pages, post_raises=False, post_status=200):
        self.pages, self.post_raises, self.post_status, self.posts = list(pages), post_raises, post_status, 0

    def get(self, url, **k):
        return self.pages.pop(0) if self.pages else Resp(200, ERROR_PAGE)

    def post(self, url, data=None, **k):
        self.posts += 1
        if self.post_raises:
            raise TimeoutError("응답 유실")
        return Resp(self.post_status, "")


def probe(pages, **kw):
    c = Crawler.__new__(Crawler)
    c.session, c._vendor_code = Sess(pages, **kw), "V"
    return c


A1 = [("A", 1, 1000)]
EXP_A1 = {"A": (1, 1000)}


def test_read_cart_rejects_non_cart_page():
    c = probe([Resp(200, ERROR_PAGE)])
    try:
        c._read_cart()
        assert False, "오류 페이지를 장바구니로 읽으면 안 된다"
    except inchun.CartReadError:
        pass


def test_read_cart_accepts_valid_empty_page():
    assert probe([Resp(200, page([]))])._read_cart()[0] == []


def test_submit_status_accepted():
    c = probe([Resp(200, page(A1)), Resp(200, page([]))])
    assert c._submit_order_status(EXP_A1) == "accepted"


def test_submit_status_rejected_even_on_http_error():
    c = probe([Resp(200, page(A1)), Resp(200, page(A1))], post_status=500)
    assert c._submit_order_status(EXP_A1) == "rejected"


def test_post_500_then_error_page_is_unknown_not_accepted():
    """3차 검수 재현: POST 500 + 조회 결과가 오류 페이지 → 주문 성공으로 보면 안 된다."""
    c = probe([Resp(200, page(A1)), Resp(200, ERROR_PAGE)], post_status=500)
    assert c._submit_order_status(EXP_A1) == "unknown"


def test_submit_status_post_exception_is_unknown():
    c = probe([Resp(200, page(A1)), Resp(200, page([]))], post_raises=True)
    assert c._submit_order_status(EXP_A1) == "unknown"


def test_submit_status_partial_is_unknown():
    two = A1 + [("B", 1, 1000)]
    c = probe([Resp(200, page(two)), Resp(200, page(A1))])
    assert c._submit_order_status({"A": (1, 1000), "B": (1, 1000)}) == "unknown"


def test_submit_status_untrusted_before_page_not_sent():
    c = probe([Resp(200, ERROR_PAGE)])
    assert c._submit_order_status(EXP_A1) == "not_sent" and c.session.posts == 0


def test_submit_status_empty_cart_not_sent():
    c = probe([Resp(200, page([]))])
    assert c._submit_order_status(EXP_A1) == "not_sent" and c.session.posts == 0


def test_submit_refuses_when_cart_differs_from_request():
    """3차 검수 재현: A 2개·B 3개 요청인데 A 만 담김 → 전송하지 않는다."""
    c = probe([Resp(200, page([("A", 2, 1000)]))])
    assert c._submit_order_status({"A": (2, 1000), "B": (3, 1000)}) == "not_sent"
    assert c.session.posts == 0


def test_submit_refuses_on_qty_or_price_mismatch():
    c = probe([Resp(200, page([("A", 1, 1000)]))])
    assert c._submit_order_status({"A": (2, 1000)}) == "not_sent"
    c = probe([Resp(200, page([("A", 1, 0)]))])
    assert c._submit_order_status({"A": (1, 1000)}) == "not_sent"
    assert c.session.posts == 0


def test_submit_refuses_foreign_item():
    c = probe([Resp(200, page([("A", 1, 1000), ("Z", 3, 500)]))])
    assert c._submit_order_status(EXP_A1) == "not_sent" and c.session.posts == 0


# ── 2) order_batch() 흐름 — _read_cart/_post_order 만 메모리로 대신한다 ─────

class FakeInchun(Crawler):
    def __init__(self, real_stock, table, saved=None, clear_fails=False,
                 lose_response_on=None, drop=()):
        self.real_stock, self.table = real_stock, table
        self.cart = list(saved or [])            # [(pc, qty)] — 단가는 1000 고정
        self.submits, self.queries = [], []
        self.clear_fails = clear_fails
        self.lose_response_on = lose_response_on  # n번째 전송은 접수되지만 응답이 유실됨
        self.drop = set(drop)                     # 담기 요청을 조용히 무시하는 품목
        self._login_id = self._login_pw = ""
        self._logged_in = True

    def ensure_login(self, *a):
        return True

    def _fetch_price(self, pid):
        return 1000

    def search(self, keyword):
        self.queries.append(keyword)
        return self.table.get(keyword, [])

    def _read_cart(self):
        lines = [(p, q, 1000) for p, q in self.cart]
        soup = bs(page(lines), "html.parser")
        return self._parse_cart(soup), soup

    def _post_order(self, data):
        self.submits.append(list(self.cart))
        if all(self.real_stock.get(p, 0) >= q for p, q in self.cart):
            self.cart = []
        if self.lose_response_on == len(self.submits):
            raise TimeoutError("접수 후 응답 유실")
        return Resp(200, "")

    def _clear_cart(self):
        if self.clear_fails:
            raise RuntimeError("장바구니 비우기 실패")
        self.cart = []

    def _add_to_cart(self, pid, qty, stock=999, price=0):
        if pid not in self.drop:
            self.cart.append((pid, int(qty)))


ITEMS = [
    {"product_id": "47523", "quantity": 2, "insurance_code": None, "product_name": "동성 정로환에프정/36T"},
    {"product_id": "53324", "quantity": 15, "insurance_code": "694003321",
     "product_name": "B대웅 베아놀점안액 0.2%/0.3ml/12EA"},
    {"product_id": "16837", "quantity": 2, "insurance_code": "653600860",
     "product_name": "$노바 엑스포지정 5/80mg//28T"},
]
REAL = {"47523": 371, "53324": 0, "16837": 261}
OK_ONLY = {"47523": 371, "53324": 100, "16837": 261}


def test_keyword_variants():
    v = Crawler._keyword_variants
    assert v("$노바 엑스포지정 5/80mg//28T")[:2] == ["엑스포지정", "엑스포"]
    assert v("B대웅 베아놀점안액 0.2%/0.3ml/12EA")[:2] == ["베아놀점안액", "베아놀"]
    assert v("한림 나조린점안액 0.5ml/10EA(반품불가)")[0] == "나조린점안액"
    assert v("동성 정로환에프정/36T")[0] == "정로환에프정"
    assert v("베아오플점안액")[0] == "베아오플점안액"
    assert v("") == []


def test_insurance_code_searched_first():
    c = FakeInchun(REAL, {"653600860": [sr("16837", 261)]})
    assert c._refetch_stock_for_item(ITEMS[2]) == 261
    assert c.queries == ["653600860"]


def test_refetch_ignores_other_pid():
    c = FakeInchun(REAL, {"베아놀점안액": [sr("38895", 500)], "베아놀": [sr("38895", 500)]})
    assert c._refetch_stock_for_item(dict(ITEMS[1], insurance_code="")) is None


def test_all_in_stock_single_submit():
    c = FakeInchun(OK_ONLY, {})
    res = c.order_batch([dict(i) for i in ITEMS])
    assert all(r.success for r in res) and len(c.submits) == 1


def test_replay_0930_refetch_finds_stockout():
    table = {"정로환에프정": [sr("47523", 371)], "694003321": [sr("53324", 0)],
             "653600860": [sr("16837", 261)]}
    res = FakeInchun(REAL, table).order_batch([dict(i) for i in ITEMS])
    assert [r.success for r in res] == [True, False, True]
    assert res[1].reason_code == "stock_zero"


def test_replay_0930_refetch_fails_phase3_isolates():
    res = FakeInchun(REAL, {}).order_batch([dict(i) for i in ITEMS])
    by = {i["product_id"]: r for i, r in zip(ITEMS, res)}
    assert by["53324"].success is False and by["53324"].reason_code == "isolated_fail"
    assert by["47523"].success and by["16837"].success


def test_phase1_unknown_is_not_resent():
    """Phase 1 이 접수됐는데 응답이 유실되면 Phase 2 로 다시 보내지 않는다."""
    c = FakeInchun(OK_ONLY, {}, lose_response_on=1)
    res = c.order_batch([dict(i) for i in ITEMS])
    assert len(c.submits) == 1
    assert {r.reason_code for r in res} == {"send_unknown"} and not any(r.success for r in res)


def test_phase2_unknown_skips_phase3():
    c = FakeInchun(REAL, {}, lose_response_on=2)
    res = c.order_batch([dict(i) for i in ITEMS])
    assert len(c.submits) == 2
    assert {r.reason_code for r in res} == {"send_unknown"}


def test_silently_dropped_item_blocks_every_send():
    """3차 검수 재현(통합): 담기가 조용히 무시된 품목이 있으면 어느 단계에서도 보내지 않는다."""
    c = FakeInchun(OK_ONLY, {}, drop={"53324"})
    res = c.order_batch([dict(i) for i in ITEMS])
    assert c.submits == []
    assert not any(r.success for r in res)


def test_phase2_clear_failure_sends_nothing():
    """약국이 담아 둔 Z 가 장바구니에 있고 비우기가 계속 실패하면 아무것도 보내지 않는다."""
    c = FakeInchun(REAL, {}, saved=[("Z", 3)], clear_fails=True)
    res = c.order_batch([dict(i) for i in ITEMS])
    assert c.submits == []
    assert not any(r.success for r in res)
    assert {r.reason_code for r in res} <= {"not_sent", "other", "stock_zero"}


def test_single_order_refuses_mismatch():
    c = FakeInchun(OK_ONLY, {}, drop={"47523"})
    r = c._order_bare("47523", 2)
    assert r.success is False and r.reason_code == "not_sent" and c.submits == []


def test_single_order_adjusted_resend_unknown_end_to_end():
    """4차 검수 재현(통합): 5개 거부 → 2개로 조정 전송 → 응답 유실.
    최종 결과는 send_unknown 이고, DB 에는 NULL, 상위 재시도 대상이 아니어야 한다."""
    from domae_mcp.cloud import scheduler as sch
    c = FakeInchun({"A": 2}, {"정로환에프정": [sr("A", 2)]}, lose_response_on=2)
    r = c.order("A", 5, product_name="동성 정로환에프정/36T")
    assert c.submits == [[("A", 5)], [("A", 2)]]
    assert r.success is False and r.reason_code == "send_unknown"
    assert sch._db_success(r) is None
    assert sch._is_item_retryable(r) is False


def test_incomplete_cart_row_is_not_sent():
    for html in [page(A1).replace('name="bagQty_0"', 'name="missing"'),
                 page(A1).replace('value="1"', 'value="oops"')]:
        c = probe([Resp(200, html)])
        assert c._submit_order_status(EXP_A1) == "not_sent"
        assert c.session.posts == 0


def test_cart_index_gap_cannot_look_empty_after_send():
    after = page([("Z", 1, 1000)]).replace('_0"', '_1"')
    c = probe([Resp(200, page(A1)), Resp(200, after)])
    assert c._submit_order_status(EXP_A1) == "unknown"


def test_changed_price_after_send_is_unknown():
    c = probe([Resp(200, page(A1)), Resp(200, page([("A", 1, 2000)]))])
    assert c._submit_order_status(EXP_A1) == "unknown"
