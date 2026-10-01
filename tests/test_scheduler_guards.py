# tests/test_scheduler_guards.py
import sys
sys.path.insert(0, "src")

from domae_mcp.core.crawlers.base import OrderResult
from domae_mcp.cloud import scheduler as sch


class Cur:
    def __init__(self, rows=None):
        self.sql = []
        self._rows = rows or [(0,)]

    def execute(self, sql, params=None):
        self.sql.append(" ".join(sql.split()))

    def fetchone(self):
        return self._rows.pop(0) if self._rows else (0,)


def test_fail_pending_rows_protects_unconfirmed():
    cur = Cur()
    sch._fail_pending_rows(cur, "b1", "워커 오류")
    assert "success IS NULL" in cur.sql[0]
    for reason in sch.UNCONFIRMED_REASONS:
        assert reason in cur.sql[0]                     # 미확정·대체 진행 중 행은 덮지 않는다


def test_finalize_protects_unconfirmed():
    cur = Cur(rows=[(1,), (0, 0, 0, 0)])
    class Conn:
        def commit(self): pass
    sch._finalize_if_confirmed(Conn(), cur, "b1", "재실행 중단")
    updates = [s for s in cur.sql if s.startswith("UPDATE domae_cloud_orders")]
    assert updates and all("send_unknown" in s and "fallback_pending" in s for s in updates)


def test_retry_excludes_unsafe_reasons():
    for rc in ("send_unknown", "isolated_fail", "not_sent", "rejected", "stock_zero", "stock_adjusted"):
        assert sch._is_item_retryable(OrderResult(success=False, reason_code=rc, message="x")) is False
    assert sch._is_item_retryable(OrderResult(success=False, message="재고 0 — 주문 누락")) is False
    assert sch._is_item_retryable(OrderResult(success=False, message="세션 만료")) is True


def test_db_success_keeps_unknown_null():
    assert sch._db_success(OrderResult(success=False, reason_code="send_unknown")) is None
    assert sch._db_success(OrderResult(success=False, reason_code="rejected")) is False
    assert sch._db_success(OrderResult(success=False, reason_code="not_sent")) is False
    assert sch._db_success(OrderResult(success=True, reason_code="ok")) is True


def test_finalize_stops_when_only_unconfirmed_rows_exist():
    cur=Cur(rows=[(1,), (0,0,0,0)])
    class Conn:
        def commit(self): pass
    assert sch._finalize_if_confirmed(Conn(),cur,'b','retry')
    assert 'send_unknown' in cur.sql[0] and 'fallback_pending' in cur.sql[0]


def test_mark_sending_is_committed_and_scoped_to_supplier():
    class Conn:
        commits=0
        def commit(self): self.commits+=1
    conn,cur=Conn(),Cur()
    sch._mark_sending(conn,cur,'b','인천')
    assert conn.commits==1
    assert 'supplier = %s' in cur.sql[0] and 'success IS NULL' in cur.sql[0]
