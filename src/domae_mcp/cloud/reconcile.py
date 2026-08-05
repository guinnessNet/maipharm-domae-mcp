"""도매몰 웹 장바구니 ↔ domae_cart_items 대조.

설계: openpharm/docs/plans/domae/CART_RECONCILE_DESIGN.md

복산처럼 SUPPORTS_CART_SYNC=True 인 도매상은 주문 전송이 **장바구니 전체를 보낸다**.
따라서 웹에만 있는 항목은 다음 주문 때 기록 없이 함께 주문된다. 이를 막기 위해
주문 직전에 양방향 대조를 하고, 대조 자체가 실패하면 주문을 중단시킨다(fail-closed).

호출자 계약
  - 이 함수는 conn 의 트랜잭션에서 쓰기만 하고 커밋하지 않는다.
  - 호출자는 **외부 주문 전송 전에 반드시 커밋**해야 한다 (설계 3.3 커밋 경계).
  - 결과의 fatal 이 설정되면 호출자는 주문을 중단해야 한다.
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


def _load_web_cart(crawler):
    """제품 단위로 합산된 웹 장바구니. 크롤러가 get_cart() 를 제공하지 않으면 직접 합산."""
    if hasattr(crawler, "get_cart"):
        return list(crawler.get_cart())
    merged = {}
    for line in crawler._get_cart_items():
        pid = line["pc"]
        entry = merged.setdefault(pid, {"product_id": pid, "quantity": 0, "price": 0})
        entry["quantity"] += int(str(line.get("qty", "0")).replace(",", "") or 0)
        entry["price"] = max(entry["price"], int(str(line.get("price", "0")).replace(",", "") or 0))
    return list(merged.values())


def _load_db_items(conn, monitor_id, supplier_name):
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


def _load_tombstones(conn, redis_client, monitor_id, supplier_name, product_ids):
    """삭제 의도 조회. 진실 원천은 DB, Redis 는 캐시일 뿐이다.

    테이블이 아직 없으면(마이그레이션 전) Redis 캐시만으로 동작한다.
    """
    tombs = set()
    cur = conn.cursor()
    try:
        cur.execute("SELECT to_regclass('public.domae_cart_deletions')")
        if cur.fetchone()[0] is not None and product_ids:
            cur.execute(
                'SELECT "productId" FROM domae_cart_deletions '
                'WHERE "monitorId" = %s AND supplier = %s AND "productId" = ANY(%s) '
                'AND "confirmedAt" IS NULL',
                (monitor_id, supplier_name, list(product_ids)),
            )
            tombs.update(r[0] for r in cur.fetchall())
    except Exception as e:
        logger.warning("삭제의도 조회 실패 (Redis 캐시로 폴백): %s", e)

    if redis_client is not None:
        for pid in product_ids:
            try:
                key = TOMBSTONE_KEY.format(monitor_id=monitor_id,
                                           supplier=supplier_name, product_id=pid)
                if redis_client.get(key):
                    tombs.add(pid)
            except Exception:
                pass
    return tombs


def _confirm_deletion(conn, monitor_id, supplier_name, product_id):
    cur = conn.cursor()
    try:
        cur.execute(
            'UPDATE domae_cart_deletions SET "confirmedAt" = now() '
            'WHERE "monitorId" = %s AND supplier = %s AND "productId" = %s '
            'AND "confirmedAt" IS NULL',
            (monitor_id, supplier_name, product_id),
        )
    except Exception as e:
        logger.warning("삭제 확인 기록 실패 (pc=%s): %s", product_id, e)


def _upsert_cart_item(conn, monitor_id, supplier_name, item):
    """고아 항목을 DB 장바구니에 기록.

    기존 실패 행(failedAt 있음)은 조회에서 빠지지만 unique 제약에는 걸리므로
    충돌 시 되살리는 필드를 전부 명시한다.
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


def _notify(conn, monitor_id, supplier_name, result):
    """대조로 바뀐 내용을 사용자에게 알린다. 실패해도 주문은 막지 않는다."""
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
    try:
        cur = conn.cursor()
        cur.execute(
            'INSERT INTO domae_notifications '
            '(id, "monitorId", type, category, title, body, data, "isRead", "createdAt") '
            "VALUES (%s, %s, 'cart_reconcile', 'domae', %s, %s, %s, false, now())",
            (notif_id, monitor_id, "%s 장바구니 자동 반영" % supplier_name, body,
             json.dumps({"added": result.added, "restored": result.restored,
                         "adjusted": result.adjusted}, ensure_ascii=False)),
        )
        result.notification_ids.append(notif_id)
    except Exception as e:
        logger.warning("대조 알림 기록 실패: %s", e)


def reconcile_cart(conn, redis_client, monitor_id, supplier_name, crawler,
                   *, in_flight_product_id=None) -> ReconcileResult:
    """웹 장바구니와 DB 장바구니를 대조하고 차이를 보정한다.

    fatal 이 설정되면 호출자는 **주문을 중단**해야 한다 (설계 3.3).
    """
    result = ReconcileResult()

    # 1. 웹 장바구니 — 못 읽으면 고아 유무를 알 수 없다 → 즉시 중단
    try:
        web_items = _load_web_cart(crawler)
    except Exception as e:
        result.fatal = "웹 장바구니 조회 실패: %s" % e
        logger.error("reconcile: %s", result.fatal)
        return result

    def _skip(pid):
        return in_flight_product_id is not None and pid == in_flight_product_id

    web = {i["product_id"]: i for i in web_items if not _skip(i["product_id"])}
    result.web_items = list(web.values())

    try:
        db = _load_db_items(conn, monitor_id, supplier_name)
    except Exception as e:
        result.fatal = "DB 장바구니 조회 실패: %s" % e
        logger.error("reconcile: %s", result.fatal)
        return result
    db = {pid: v for pid, v in db.items() if not _skip(pid)}

    tombs = _load_tombstones(conn, redis_client, monitor_id, supplier_name,
                             set(web) | set(db))

    # 2. 웹에만 있는 항목
    for pid in sorted(set(web) - set(db)):
        item = web[pid]
        if pid in tombs:
            # 사용자가 지운 항목이 웹에 남아있다 — 되살리지 말고 웹에서 제거
            try:
                crawler.remove_from_cart(pid)
                still = any(i["product_id"] == pid for i in _load_web_cart(crawler))
                if still:
                    raise RuntimeError("삭제 후에도 장바구니에 잔존")
                _confirm_deletion(conn, monitor_id, supplier_name, pid)
            except Exception as e:
                result.fatal = "삭제 대상 품목 제거 실패 (pc=%s): %s" % (pid, e)
                logger.error("reconcile: %s", result.fatal)
                return result
            continue
        try:
            _upsert_cart_item(conn, monitor_id, supplier_name, item)
            result.added.append(item)
        except Exception as e:
            result.fatal = "고아 항목 기록 실패 (pc=%s): %s" % (pid, e)
            logger.error("reconcile: %s", result.fatal)
            return result

    # 3. DB에만 있는 항목 → 재담기 (실패해도 개별 실패로만 처리)
    for pid in sorted(set(db) - set(web)):
        row = db[pid]
        try:
            crawler._add_to_cart(pid, row["quantity"], price=row.get("price") or 0)
            present = any(i["product_id"] == pid for i in _load_web_cart(crawler))
            if not present:
                raise RuntimeError("담기 후에도 장바구니에 없음")
            cur = conn.cursor()
            cur.execute(
                'UPDATE domae_cart_items SET "syncStatus" = %s, "syncError" = NULL, '
                '"syncedAt" = now(), "updatedAt" = now() WHERE id = %s',
                ("synced", row["id"]))
            result.restored.append(row)
        except Exception as e:
            logger.warning("reconcile 재담기 실패 (pc=%s): %s", pid, e)
            try:
                cur = conn.cursor()
                cur.execute(
                    'UPDATE domae_cart_items SET "syncStatus" = %s, "syncError" = %s, '
                    '"failedAt" = now(), "failReason" = %s, "updatedAt" = now() '
                    'WHERE id = %s',
                    ("failed", str(e)[:200], "대조 재담기 실패", row["id"]))
            except Exception:
                pass
            result.failed.append({"product_id": pid, "reason": str(e)[:200]})

    # 4. 양쪽에 있으나 수량이 다름 → 실제 선점된 웹 기준으로 DB 보정
    for pid in sorted(set(web) & set(db)):
        web_qty = int(web[pid].get("quantity") or 0)
        db_qty = int(db[pid].get("quantity") or 0)
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

    # 5. 재검증 — 대조와 전송 사이에 상태가 바뀌면 결과가 무효다
    try:
        final_items = _load_web_cart(crawler)
    except Exception as e:
        result.fatal = "재검증 조회 실패: %s" % e
        logger.error("reconcile: %s", result.fatal)
        return result
    final_pids = {i["product_id"] for i in final_items if not _skip(i["product_id"])}
    expected = (set(web) | set(db)) - tombs - {f["product_id"] for f in result.failed}
    if final_pids != expected:
        result.fatal = "재검증 불일치 (기대 %s / 실제 %s)" % (
            sorted(expected), sorted(final_pids))
        logger.error("reconcile: %s", result.fatal)
        return result
    result.web_items = [i for i in final_items if not _skip(i["product_id"])]

    _notify(conn, monitor_id, supplier_name, result)

    if result.changed:
        logger.info("reconcile[%s]: 고아 %d · 재담기 %d · 수량보정 %d · 실패 %d",
                    supplier_name, len(result.added), len(result.restored),
                    len(result.adjusted), len(result.failed))
    return result
