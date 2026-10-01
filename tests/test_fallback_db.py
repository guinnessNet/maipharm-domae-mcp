# tests/test_fallback_db.py
import sys
sys.path.insert(0, "src")

import pytest

from domae_mcp.core.crawlers.base import OrderResult, SearchResult
from domae_mcp.cloud.fallback_db import FallbackRecorder


class Cur:
    def __init__(self, conn):
        self.conn = conn

    def execute(self, sql, params=None):
        flat = " ".join(sql.split())
        if self.conn.fail_on and self.conn.fail_on in flat:
            raise RuntimeError("SQL 오류")
        self.conn.sql.append((flat, params))


class Conn:
    def __init__(self, fail_on=None):
        self.sql, self.fail_on = [], fail_on
        self.commits = self.rollbacks = 0

    def cursor(self):
        return Cur(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


PICK = SearchResult(maker="", product_name="베아놀", unit="12EA", insurance_code="694003321",
                    quantity=30, supplier="복산", price=1000, product_id="b1")


def _rec(conn, record_fn=None):
    return FallbackRecorder(conn, "m1", "b1", "인천",
                            record_fn or (lambda cur, *a, **k: cur.execute("UPDATE domae_cloud_orders SET x", None)),
                            id_fn=lambda: "row1", now_fn=lambda: "NOW")


def test_pending_inserts_null_with_marker_and_commits():
    conn = Conn()
    assert _rec(conn).pending({}, "복산", PICK, 15) == "row1"
    sql, params = conn.sql[0]
    assert sql.startswith("INSERT INTO domae_cloud_orders") and "NULL" in sql
    assert "fallback_pending" in params and conn.commits == 1


def test_unconfirmed_keeps_success_null():
    conn = Conn()
    _rec(conn).unconfirmed("row1", "확인 필요")
    sql, params = conn.sql[0]
    assert "success =" not in sql.replace("success IS NULL", "")
    assert "send_unknown" in params and "success IS NULL" in sql and conn.commits == 1


def test_sql_error_rolls_back_and_raises():
    conn = Conn(fail_on="INSERT INTO domae_cloud_orders")
    with pytest.raises(RuntimeError):
        _rec(conn).pending({}, "복산", PICK, 15)
    assert conn.rollbacks == 1 and conn.commits == 0


def test_result_uses_record_fn_with_row_id():
    seen = []
    conn = Conn()
    _rec(conn, lambda cur, *a, **k: seen.append((a, k))).result("row1", {}, "복산", OrderResult(success=True))
    (args, kw), = seen
    assert args[3] == {"db_order_id": "row1"} and kw["success"] is True and conn.commits == 1


def test_apply_cart():
    conn = Conn()
    r = _rec(conn)
    r.apply_cart("c1", "delete", 0, "")
    r.apply_cart("c1", "keep_failed", 2, "남은 2개")
    r.apply_cart("c1", "note", 0, "확인 필요")
    r.apply_cart("c1", "none", 0, "")
    kinds = [s.split()[0] for s, _ in conn.sql]
    assert kinds == ["DELETE", "UPDATE", "UPDATE"]
    assert "quantity" in conn.sql[1][0] and "quantity" not in conn.sql[2][0]
