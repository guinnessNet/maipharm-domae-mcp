# src/domae_mcp/cloud/fallback_db.py
"""대체주문 DB 기록기 — auto_order 의 연결과 분리된 전용 연결을 쓴다.

SQL 오류가 나면 이 연결만 rollback 하고 예외를 다시 던진다(run_fallback 이 그 예외로
"전송 안 함" 또는 "미확정"을 판단한다). 원 주문 연결의 트랜잭션은 오염되지 않는다.
"""


class FallbackRecorder:
    def __init__(self, conn, monitor_id, batch_id, origin_supplier, record_fn, id_fn, now_fn):
        self.conn = conn
        self.monitor_id = monitor_id
        self.batch_id = batch_id
        self.origin = origin_supplier
        self._record = record_fn      # scheduler._record_order_result
        self._id = id_fn              # scheduler._generate_cuid
        self._now = now_fn

    def _tx(self, fn):
        try:
            cur = self.conn.cursor()
            out = fn(cur)
            self.conn.commit()
            return out
        except Exception:
            try:
                self.conn.rollback()
            except Exception:
                pass
            raise

    def pending(self, item, sup, pick, qty) -> str:
        """주문 전송 전에 남기는 흔적. 크래시 시 success IS NULL + fallback_pending 으로 남는다."""
        row_id = self._id()

        def _do(cur):
            cur.execute("""
                INSERT INTO domae_cloud_orders
                (id, "monitorId", "batchId", supplier, "productName", unit, "insuranceCode",
                 quantity, price, success, "productId", "orderId", message, "reasonCode", "orderedAt")
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,NULL,%s,'',%s,%s,%s)
            """, (row_id, self.monitor_id, self.batch_id, sup, pick.product_name, pick.unit,
                  pick.insurance_code, qty, pick.price, pick.product_id,
                  f"대체주문 진행 중 ({self.origin} 품절)", "fallback_pending", self._now()))
            return row_id

        return self._tx(_do)

    def result(self, row_id, item, sup, res) -> None:
        """확정 결과(성공, not_sent, rejected) 기록."""
        self._tx(lambda cur: self._record(
            cur, self.monitor_id, self.batch_id, sup, {"db_order_id": row_id},
            success=res.success,
            message=(f"대체주문 ({self.origin} 품절) — " + (res.message or "")).strip(),
            order_id=getattr(res, "order_id", None),
            adjusted_qty=getattr(res, "adjusted_quantity", None),
            avail_stock=getattr(res, "available_stock", None),
            reason_code=getattr(res, "reason_code", None)))

    def unconfirmed(self, row_id, message) -> None:
        """접수 여부 불명 — success 는 NULL 그대로 두고 사유만 남긴다."""
        self._tx(lambda cur: cur.execute(
            'UPDATE domae_cloud_orders SET message = %s, "reasonCode" = %s '
            'WHERE id = %s AND success IS NULL',
            (message, "send_unknown", row_id)))

    def apply_cart(self, cart_item_id, action, qty, why) -> None:
        if not cart_item_id or action == "none":
            return
        if action == "delete":
            sql, params = 'DELETE FROM domae_cart_items WHERE id = %s', (cart_item_id,)
        elif action == "keep_failed":
            sql, params = ('UPDATE domae_cart_items SET quantity = %s, "failReason" = %s WHERE id = %s',
                           (qty, why, cart_item_id))
        else:  # note
            sql, params = 'UPDATE domae_cart_items SET "failReason" = %s WHERE id = %s', (why, cart_item_id)
        self._tx(lambda cur: cur.execute(sql, params))
