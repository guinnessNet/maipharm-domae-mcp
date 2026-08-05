"""도매몰 웹 장바구니 ↔ domae_cart_items 대조.

설계: openpharm/docs/plans/domae/CART_RECONCILE_DESIGN.md

복산처럼 SUPPORTS_CART_SYNC=True 인 도매상은 주문 전송이 **장바구니 전체를 보낸다**.
따라서 웹에만 있는 항목은 다음 주문 때 기록 없이 함께 주문된다. 이를 막기 위해
주문 직전에 양방향 대조를 하고, 대조 자체가 실패하면 주문을 중단시킨다(fail-closed).

호출자 계약
  - 이 함수는 conn 의 트랜잭션에서 쓰기만 하고 커밋하지 않는다.
  - 호출자는 **외부 주문 전송 전에 반드시 커밋**해야 한다 (설계 3.3 커밋 경계).
  - fatal 이 설정되면 호출자는 주문을 중단하고 rollback 해야 한다.

트랜잭션 주의
  PostgreSQL 은 SQL 하나가 실패하면 트랜잭션 전체가 aborted 가 된다. 따라서
  진실 원천(장바구니·삭제의도) 관련 SQL 실패는 **삼키지 않고 fatal** 로 올린다.
  알림처럼 부가적인 쓰기만 SAVEPOINT 로 감싸 실패해도 본 트랜잭션을 살린다.
"""
import json
import logging
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

TOMBSTONE_KEY = "domae:cart:tombstone:{monitor_id}:{supplier}:{product_id}"


@dataclass
class ReconcileResult:
    added: list = field(default_factory=list)        # 웹→DB 로 기록한 고아
    restored: list = field(default_factory=list)     # DB→웹 으로 재담기 성공
    adjusted: list = field(default_factory=list)     # 수량 보정
    failed: list = field(default_factory=list)       # 개별 처리 실패
    fatal: Optional[str] = None                      # 설정되면 호출자는 주문 중단
    web_items: list = field(default_factory=list)    # 대조 시점 웹 카트 (제품 단위 합산)
    notification_ids: list = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.added or self.restored or self.adjusted)


def _cuid():
    from domae_mcp.cloud.scheduler import _generate_cuid
    return _generate_cuid()


def _qty(value) -> int:
    try:
        return int(str(value).replace(",", "") or 0)
    except (TypeError, ValueError):
        return 0


def _load_web_cart(crawler) -> dict:
    """제품 단위로 합산된 웹 장바구니 {product_id: {...}}."""
    if hasattr(crawler, "get_cart"):
        rows = list(crawler.get_cart())
    else:
        merged = {}
        for line in crawler._get_cart_items():
            pid = line["pc"]
            entry = merged.setdefault(pid, {"product_id": pid, "quantity": 0, "price": 0})
            entry["quantity"] += _qty(line.get("qty"))
            entry["price"] = max(entry["price"], _qty(line.get("price")))
        rows = list(merged.values())
    return {r["product_id"]: r for r in rows}


def _load_db_items(conn, monitor_id, supplier_name) -> dict:
    cur = conn.cursor()
    cur.execute(
        'SELECT id, "productId", quantity, "productName", price '
        'FROM domae_cart_items '
        'WHERE "monitorId" = %s AND supplier = %s AND "productId" IS NOT NULL '
        'AND "failedAt" IS NULL',
        (monitor_id, supplier_name),
    )
    return {
        r[1]: {"id": r[0], "product_id": r[1], "quantity": r[2],
               "product_name": r[3], "price": r[4]}
        for r in cur.fetchall()
    }


def _load_tombstones(conn, redis_client, monitor_id, supplier_name, product_ids) -> set:
    """미확인 삭제 의도. 진실 원천은 DB, Redis 는 캐시.

    DB 조회 실패는 삼키지 않는다 — 삭제 의도를 모르는 채 진행하면
    사용자가 지운 약을 되살려 주문하게 된다. 예외를 그대로 올린다.
    """
    tombs = set()
    cur = conn.cursor()
    cur.execute("SELECT to_regclass('public.domae_cart_deletions')")
    has_table = cur.fetchone()[0] is not None
    if has_table and product_ids:
        cur.execute(
            'SELECT "productId" FROM domae_cart_deletions '
            'WHERE "monitorId" = %s AND supplier = %s AND "productId" = ANY(%s) '
            'AND "confirmedAt" IS NULL',
            (monitor_id, supplier_name, list(product_ids)),
        )
        tombs.update(r[0] for r in cur.fetchall())

    if redis_client is not None:
        for pid in product_ids:
            try:
                key = TOMBSTONE_KEY.format(monitor_id=monitor_id,
                                           supplier=supplier_name, product_id=pid)
                if redis_client.get(key):
                    tombs.add(pid)
            except Exception as e:
                # Redis 는 캐시일 뿐이므로 조회 실패는 치명적이지 않다.
                logger.warning("tombstone 캐시 조회 실패 (pc=%s): %s", pid, e)
    return tombs


def _confirm_deletion(conn, redis_client, monitor_id, supplier_name, product_id):
    """웹에서 사라진 것을 확인했으므로 삭제 의도를 확정한다.

    기록 실패를 삼키면 의도가 영원히 미확인으로 남아 나중에 정상 추가한 품목까지
    삭제 대상으로 취급된다. 예외를 그대로 올린다.
    """
    cur = conn.cursor()
    cur.execute("SELECT to_regclass('public.domae_cart_deletions')")
    if cur.fetchone()[0] is not None:
        cur.execute(
            'UPDATE domae_cart_deletions SET "confirmedAt" = now() '
            'WHERE "monitorId" = %s AND supplier = %s AND "productId" = %s '
            'AND "confirmedAt" IS NULL',
            (monitor_id, supplier_name, product_id),
        )
    if redis_client is not None:
        try:
            redis_client.delete(TOMBSTONE_KEY.format(
                monitor_id=monitor_id, supplier=supplier_name, product_id=product_id))
        except Exception:
            pass


def _upsert_cart_item(conn, monitor_id, supplier_name, item) -> str:
    """고아 항목을 DB 장바구니에 기록하고 **행 id 를 돌려준다**.

    id 를 돌려주지 않으면 호출자가 주문 성공 후 그 장바구니 행을 지우지 못해,
    다음 대조가 같은 품목을 "DB에만 있음"으로 보고 웹에 다시 담는 루프가 생긴다.
    """
    cur = conn.cursor()
    cur.execute(
        'INSERT INTO domae_cart_items '
        '(id, "monitorId", supplier, "productId", "productName", quantity, price, '
        ' "syncStatus", "syncedAt", "createdAt", "updatedAt") '
        "VALUES (%s, %s, %s, %s, %s, %s, %s, 'synced', now(), now(), now()) "
        'ON CONFLICT ("monitorId", "productId", supplier) DO UPDATE SET '
        '  quantity = EXCLUDED.quantity, '
        '  price = EXCLUDED.price, '
        '  "productName" = EXCLUDED."productName", '
        "  \"syncStatus\" = 'synced', "
        '  "syncedAt" = now(), '
        '  "syncError" = NULL, '
        '  "failedAt" = NULL, '
        '  "failReason" = NULL, '
        '  "updatedAt" = now()',
        (_cuid(), monitor_id, supplier_name, item["product_id"],
         item.get("product_name") or item["product_id"],
         item.get("quantity") or 0, item.get("price") or 0),
    )
    cur.execute(
        'SELECT id FROM domae_cart_items WHERE "monitorId" = %s AND "productId" = %s '
        'AND supplier = %s',
        (monitor_id, item["product_id"], supplier_name))
    row = cur.fetchone()
    return row[0] if row else None


def _in_savepoint(conn, name, fn):
    """부가 쓰기를 SAVEPOINT 로 감싼다 — 실패해도 본 트랜잭션은 살아남는다."""
    cur = conn.cursor()
    cur.execute("SAVEPOINT %s" % name)
    try:
        fn(cur)
    except Exception as e:
        cur.execute("ROLLBACK TO SAVEPOINT %s" % name)
        logger.warning("%s 실패 (본 트랜잭션은 유지): %s", name, e)
        return False
    cur.execute("RELEASE SAVEPOINT %s" % name)
    return True


def _mark_item_failed(conn, item_id, reason):
    def _do(cur):
        cur.execute(
            'UPDATE domae_cart_items SET "syncStatus" = %s, "syncError" = %s, '
            '"failedAt" = now(), "failReason" = %s, "updatedAt" = now() WHERE id = %s',
            ("failed", str(reason)[:200], "대조 재담기 실패", item_id))
    _in_savepoint(conn, "sp_mark_failed", _do)


def _notify(conn, monitor_id, supplier_name, result):
    """대조로 바뀐 내용을 알린다. 부가 기능이라 실패해도 주문은 막지 않는다."""
    if not result.changed:
        return
    parts = []
    if result.added:
        parts.append("도매몰에만 있던 %d건 반영" % len(result.added))
    if result.restored:
        parts.append("누락된 %d건 재담기" % len(result.restored))
    if result.adjusted:
        parts.append("수량 %d건 보정" % len(result.adjusted))
    body = "%s 장바구니 자동 반영 — %s" % (supplier_name, ", ".join(parts))
    notif_id = _cuid()

    def _do(cur):
        cur.execute(
            'INSERT INTO domae_notifications '
            '(id, "monitorId", type, category, title, body, data, "isRead", "createdAt") '
            "VALUES (%s, %s, 'cart_reconcile', 'domae', %s, %s, %s, false, now())",
            (notif_id, monitor_id, "%s 장바구니 자동 반영" % supplier_name, body,
             json.dumps({"added": result.added, "restored": result.restored,
                         "adjusted": result.adjusted}, ensure_ascii=False)))

    if _in_savepoint(conn, "sp_notify", _do):
        result.notification_ids.append(notif_id)


def reconcile_cart(conn, redis_client, monitor_id, supplier_name, crawler,
                   *, in_flight_product_id=None) -> ReconcileResult:
    """웹 장바구니와 DB 장바구니를 대조하고 차이를 보정한다.

    fatal 이 설정되면 호출자는 **주문을 중단**해야 한다 (설계 3.3).
    """
    result = ReconcileResult()

    def _skip(pid):
        return in_flight_product_id is not None and pid == in_flight_product_id

    # ── 1. 웹 장바구니 ──────────────────────────────────
    try:
        web = {p: v for p, v in _load_web_cart(crawler).items() if not _skip(p)}
    except Exception as e:
        result.fatal = "웹 장바구니 조회 실패: %s" % e
        logger.error("reconcile: %s", result.fatal)
        return result
    result.web_items = list(web.values())

    # ── 2. DB 장바구니 ──────────────────────────────────
    try:
        db = {p: v for p, v in _load_db_items(conn, monitor_id, supplier_name).items()
              if not _skip(p)}
    except Exception as e:
        result.fatal = "DB 장바구니 조회 실패: %s" % e
        logger.error("reconcile: %s", result.fatal)
        return result

    # ── 3. 삭제 의도 (진실 원천 조회 실패는 fatal) ──────────
    try:
        tombs = _load_tombstones(conn, redis_client, monitor_id, supplier_name,
                                 set(web) | set(db))
    except Exception as e:
        result.fatal = "삭제 의도 조회 실패: %s" % e
        logger.error("reconcile: %s", result.fatal)
        return result

    # ── 4. tombstone 우선 처리 (DB 존재 여부와 무관) ────────
    # 삭제 의도가 있는 품목은 웹에서 없애고, 절대 재담기하지 않는다.
    for pid in sorted(tombs & (set(web) | set(db))):
        if pid in web:
            try:
                crawler.remove_from_cart(pid)
                if pid in _load_web_cart(crawler):
                    raise RuntimeError("삭제 후에도 장바구니에 잔존")
            except Exception as e:
                result.fatal = "삭제 대상 품목 제거 실패 (pc=%s): %s" % (pid, e)
                logger.error("reconcile: %s", result.fatal)
                return result
            web.pop(pid, None)
        try:
            _confirm_deletion(conn, redis_client, monitor_id, supplier_name, pid)
        except Exception as e:
            result.fatal = "삭제 확인 기록 실패 (pc=%s): %s" % (pid, e)
            logger.error("reconcile: %s", result.fatal)
            return result
        if pid in db:
            # 사용자가 지웠는데 DB 행이 남아있다 — 주문 대상에서 빼되 데이터는 지우지 않는다.
            _mark_item_failed(conn, db[pid]["id"], "삭제 의도 있는 품목")
            result.failed.append({"product_id": pid, "reason": "삭제 의도 있는 품목"})
            db.pop(pid, None)

    # 재검증에서 기대할 최종 상태 {제품: 수량}
    expected = {pid: _qty(v.get("quantity")) for pid, v in web.items()}

    # ── 5. 웹에만 있는 항목 → DB 기록 ─────────────────────
    for pid in sorted(set(web) - set(db)):
        try:
            new_id = _upsert_cart_item(conn, monitor_id, supplier_name, web[pid])
            web[pid]["cart_item_id"] = new_id
            result.added.append(web[pid])
        except Exception as e:
            result.fatal = "고아 항목 기록 실패 (pc=%s): %s" % (pid, e)
            logger.error("reconcile: %s", result.fatal)
            return result

    # ── 6. DB에만 있는 항목 → 재담기 (개별 실패 허용) ────────
    for pid in sorted(set(db) - set(web)):
        row = db[pid]
        want = _qty(row.get("quantity"))
        try:
            crawler._add_to_cart(pid, want, price=row.get("price") or 0)
            got = _qty(_load_web_cart(crawler).get(pid, {}).get("quantity"))
            if got != want:
                raise RuntimeError("담긴 수량 불일치 (기대 %d / 실제 %d)" % (want, got))
            cur = conn.cursor()
            cur.execute(
                'UPDATE domae_cart_items SET "syncStatus" = %s, "syncError" = NULL, '
                '"syncedAt" = now(), "updatedAt" = now() WHERE id = %s',
                ("synced", row["id"]))
            result.restored.append(row)
            expected[pid] = want
        except Exception as e:
            logger.warning("reconcile 재담기 실패 (pc=%s): %s", pid, e)
            _mark_item_failed(conn, row["id"], e)
            result.failed.append({"product_id": pid, "reason": str(e)[:200]})

    # ── 7. 양쪽 존재, 수량 불일치 → 웹 기준으로 DB 보정 ──────
    for pid in sorted(set(web) & set(db)):
        web_qty, db_qty = _qty(web[pid].get("quantity")), _qty(db[pid].get("quantity"))
        if web_qty == db_qty:
            continue
        try:
            cur = conn.cursor()
            cur.execute(
                'UPDATE domae_cart_items SET quantity = %s, "syncStatus" = %s, '
                '"syncError" = NULL, "syncedAt" = now(), "updatedAt" = now() WHERE id = %s',
                (web_qty, "synced", db[pid]["id"]))
            result.adjusted.append({"product_id": pid, "from": db_qty, "to": web_qty})
        except Exception as e:
            result.fatal = "수량 보정 실패 (pc=%s): %s" % (pid, e)
            logger.error("reconcile: %s", result.fatal)
            return result

    # ── 8. 재검증 — 제품뿐 아니라 수량까지 일치해야 한다 ──────
    # 제품 집합만 보면 2개가 20개로 바뀐 것을 놓치고, DB 이력과 실제 전송량이 어긋난다.
    try:
        final = {p: v for p, v in _load_web_cart(crawler).items() if not _skip(p)}
    except Exception as e:
        result.fatal = "재검증 조회 실패: %s" % e
        logger.error("reconcile: %s", result.fatal)
        return result
    # 호출자가 주문 성공 후 DB 장바구니 행을 지울 수 있도록 id 를 실어준다.
    id_map = {pid: row["id"] for pid, row in db.items()}
    for a in result.added:
        if a.get("cart_item_id"):
            id_map[a["product_id"]] = a["cart_item_id"]
    for pid, v in final.items():
        if pid in id_map:
            v["cart_item_id"] = id_map[pid]

    actual = {pid: _qty(v.get("quantity")) for pid, v in final.items()}
    if actual != expected:
        result.fatal = "재검증 불일치 (기대 %s / 실제 %s)" % (
            sorted(expected.items()), sorted(actual.items()))
        logger.error("reconcile: %s", result.fatal)
        return result
    result.web_items = list(final.values())

    _notify(conn, monitor_id, supplier_name, result)

    if result.changed or result.failed:
        logger.info("reconcile[%s]: 고아 %d · 재담기 %d · 수량보정 %d · 실패 %d",
                    supplier_name, len(result.added), len(result.restored),
                    len(result.adjusted), len(result.failed))
    return result
