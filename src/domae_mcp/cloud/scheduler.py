"""클라우드 모니터링 스케줄러"""
import hashlib
import html
import hmac
import importlib.util
import json
import logging
import os
import re
import secrets
import string
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from concurrent.futures import TimeoutError as FuturesTimeoutError
from datetime import datetime, timedelta, timezone

import psycopg2

from domae_mcp.core.crawlers.base import CrawlerError, OrderResult, confirmed_quantity
from domae_mcp.core.crawlers.cart_snapshot import CartSnapshot, RELEASE_PENDING_KEY
from domae_mcp.cloud.fallback import (NEEDS_CHECK_STATES, cart_action_after_fallback, cart_action_after_order, fallback_need_qty, next_suppliers, run_fallback, format_ordered_line)
from domae_mcp.cloud.fallback_db import FallbackRecorder
from domae_mcp.cloud.reconcile import reconcile_cart


def _generate_cuid() -> str:
    """Prisma cuid() 호환 ID 생성 (25자, 'c'로 시작)."""
    ts = int(time.time() * 1000)
    ts_part = ""
    base = 36
    while ts > 0:
        char = string.digits[ts % base] if ts % base < 10 else chr(ord('a') + ts % base - 10)
        ts_part = char + ts_part
        ts //= base
    rand_part = secrets.token_hex(12)[:16]  # 16자 cryptographically secure random
    return f"c{ts_part}{rand_part}"[:25]

logger = logging.getLogger(__name__)

# 전 도매 검색이 끝나기를 기다리는 상한(초). 정상값은 55품목·5도매 기준 약 13초라
# 20배 이상 여유. 초과분은 그 도매만 결과에서 빠지고 나머지는 정상 반영된다.
# 이 시한이 없으면 응답 없는 도매 하나가 워커 전체를 세운다.
SUPPLIER_SCAN_TIMEOUT = 300

# ─── 장바구니 락 / 지연 재큐잉 (설계: CART_RECONCILE_DESIGN.md 3.4) ───
CART_LOCK_TTL = 120
DELAYED_QUEUE = "domae:jobs:delayed"
MAX_REQUEUE = 3
REQUEUE_BACKOFF = [5, 15, 45]

_LOCK_RELEASE_LUA = ("if redis.call('get', KEYS[1]) == ARGV[1] then "
                     "return redis.call('del', KEYS[1]) else return 0 end")
_LOCK_RENEW_LUA = ("if redis.call('get', KEYS[1]) == ARGV[1] then "
                   "return redis.call('pexpire', KEYS[1], ARGV[2]) else return 0 end")


class _LockUnavailable(Exception):
    """공급사 락을 못 잡았다 — 주문하지 말고 재큐잉해야 한다."""

    def __init__(self, supplier):
        super().__init__("%s 장바구니 락 획득 실패" % supplier)
        self.supplier = supplier


class _ReconcileFatal(Exception):
    """대조가 fail-closed 판정 — 주문을 중단해야 한다."""


def _cart_lock_key(monitor_id: str, supplier: str) -> str:
    return f"domae:cart:lock:{monitor_id}:{supplier}"


def _acquire_cart_lock(redis_client, monitor_id: str, supplier: str, retries: int = 1):
    """고유 토큰으로 락 획득. 실패 시 None — 호출자는 주문을 진행하면 안 된다."""
    token = _generate_cuid()
    key = _cart_lock_key(monitor_id, supplier)
    for attempt in range(retries + 1):
        if redis_client.set(key, token, nx=True, ex=CART_LOCK_TTL):
            return token
        if attempt < retries:
            time.sleep(3)
    return None


def _release_cart_lock(redis_client, monitor_id: str, supplier: str, token):
    """토큰이 일치할 때만 해제. 만료 후 남의 락을 지우지 않는다."""
    if not token:
        return
    try:
        redis_client.eval(_LOCK_RELEASE_LUA, 1, _cart_lock_key(monitor_id, supplier), token)
    except Exception as e:
        logger.warning("장바구니 락 해제 실패 [%s]: %s", supplier, e)


def _renew_cart_lock(redis_client, monitor_id: str, supplier: str, token) -> bool:
    """전송 직전 lease 연장. 이미 남의 소유면 False → 주문 중단해야 한다."""
    if not token:
        return False
    try:
        return bool(redis_client.eval(_LOCK_RENEW_LUA, 1,
                                      _cart_lock_key(monitor_id, supplier),
                                      token, CART_LOCK_TTL * 1000))
    except Exception as e:
        logger.warning("장바구니 락 연장 실패 [%s]: %s", supplier, e)
        return False


def _requeue_delayed(redis_client, job: dict, reason: str) -> bool:
    """지연 큐(ZSET)로 재큐잉. Redis list 에는 지연이 없어 즉시 재소비되므로 ZSET 을 쓴다.

    반환 False = 재시도 소진 → 호출자가 실패로 마감해야 한다.
    """
    retry = int(job.get("retry_count", 0))
    if retry >= MAX_REQUEUE:
        logger.error("재큐잉 소진 (%d회) — %s", retry, reason)
        return False
    delay = REQUEUE_BACKOFF[min(retry, len(REQUEUE_BACKOFF) - 1)]
    new_job = dict(job)
    new_job["retry_count"] = retry + 1
    try:
        redis_client.zadd(DELAYED_QUEUE,
                          {json.dumps(new_job, ensure_ascii=False): time.time() + delay})
        logger.info("재큐잉 %d회차 (%ds 후): %s", retry + 1, delay, reason)
        return True
    except Exception as e:
        logger.error("재큐잉 실패: %s", e)
        return False


# 전송했을 수 있는 미확정 행. 배치 일괄 실패 처리가 이 행을 false 로 덮으면
# "주문 안 됨"으로 보이고 대조(reconcile)의 재담기 차단도 풀려 이중 주문으로 이어진다.
UNCONFIRMED_REASONS = ("send_unknown", "fallback_pending")
_KEEP_UNCONFIRMED = ('AND ("reasonCode" IS NULL OR "reasonCode" NOT IN '
                     "('send_unknown', 'fallback_pending'))")



def _db_success(result):
    """domae_cloud_orders.success 에 넣을 값. 접수 여부 불명(send_unknown)은 확정 실패가 아니므로 NULL."""
    if getattr(result, "reason_code", None) == "send_unknown":
        return None
    return bool(result.success)


def _retry_tag(result) -> str:
    return " (재시도 후 주문)" if getattr(result, "retried", False) else ""


def _quick_order_message(supplier, product_name, quantity, result, db_success) -> str:
    """바로주문 텔레그램 문구. 수량 조정으로 덜 주문됐으면 실제 주문 수량을 적는다."""
    if db_success is None:
        return f"⚠ [{supplier}] {product_name} 전송 결과 확인 필요 — 도매몰 주문내역을 확인하세요"
    if db_success:
        got = getattr(result, "adjusted_quantity", None) or quantity
        short = f" (요청 {quantity}개, 재고 부족)" if got < quantity else ""
        return f"✅ [{supplier}] {product_name} {got}개 주문 완료{short}{_retry_tag(result)}"
    return f"❌ [{supplier}] {product_name} 주문 실패: {getattr(result, 'message', '')}"


def _batch_success_line(supplier, item, result) -> str:
    """일괄주문 성공 품목 한 줄 (수량 조정 제외)."""
    return f" · [{supplier}] {item.get('product_name', '')} ×{int(item.get('quantity', 1))}{_retry_tag(result)}"


def _is_item_retryable(result) -> bool:
    """batch_order 의 품목별 1회 재시도 대상인가. send_unknown 을 다시 보내면 이중 주문이 된다."""
    from domae_mcp.core.crawlers.base import SAFE_RESEND_REASONS
    return (not result.success
            and getattr(result, "reason_code", None) in SAFE_RESEND_REASONS
            and getattr(result, "fulfilled_quantity", 0) == 0
            and not getattr(result, "no_retry", False))


CART_SENDING_REASON = '전송 결과 확인 중'


def _mark_sending(conn, cur, batch_id, supplier, cart_item_ids=()):
    """외부 전송 전 보호 표식을 커밋한다. 결과 기록 실패/죽음에도 재전송하지 않는다.

    이번에 실제로 전송하는 장바구니 행(cart_item_ids)도 **같은 트랜잭션**에서 실패 상태로
    묶는다. 서버 자동주문 크론은 failedAt IS NULL 만 다시 주문하므로, 전송 뒤 결과 기록 전에
    죽어도 다음 슬롯이 같은 품목을 재주문하지 않는다. 약국·도매 단위로 묶지 않는다
    (이번 요청 밖 품목까지 막힌다). 결과 루프가 이후 삭제·잔량·실패로 덮어쓴다.
    커밋이 실패하면 예외가 나가고 호출자는 전송하지 않는다.
    """
    cur.execute('UPDATE domae_cloud_orders SET "reasonCode" = %s, message = %s '
                'WHERE "batchId" = %s AND supplier = %s AND success IS NULL',
                ('send_unknown', '전송 결과 확인 필요 — 주문 실행 중', batch_id, supplier))
    ids = [i for i in dict.fromkeys(cart_item_ids or ()) if i]
    if ids:
        cur.execute('UPDATE domae_cart_items SET "failedAt" = %s, "failReason" = %s WHERE id = ANY(%s)',
                    (datetime.now(timezone.utc).replace(tzinfo=None), CART_SENDING_REASON, ids))
    conn.commit()


def _cart_ids(items):
    return [it.get("cart_item_id") for it in items if it.get("cart_item_id")]


def _close_batch(cur, batch_id, done_status="completed", adjusted=None, missing=None):
    """원 주문·대체주문이 모두 끝난 뒤 배치를 **DB 행 기준**으로 마감한다. 커밋은 호출자가 한다.

    미확정(success IS NULL + send_unknown/fallback_pending)은 실패로 세지 않는다. 남아 있으면
    배치를 processing, completedAt=NULL 로 둔다 — 서버 리퍼가 보호 행을 열어 둔 채 관찰한다.
    반환: (성공, 확정 실패, 미확정) 행 수
    """
    cur.execute("""
        SELECT count(*) FILTER (WHERE success),
               count(*) FILTER (WHERE success = false),
               count(*) FILTER (WHERE success IS NULL
                                AND "reasonCode" IN ('send_unknown', 'fallback_pending'))
        FROM domae_cloud_orders WHERE "batchId" = %s
    """, (batch_id,))
    ok, ng, unconfirmed = cur.fetchone()
    status = "processing" if unconfirmed else done_status
    sets = ['status = %s', '"completedAt" = ' + ('NULL' if unconfirmed else 'now()'),
            '"successCount" = %s', '"failCount" = %s']
    params = [status, ok, ng]
    if adjusted is not None:
        sets.append('"adjustedCount" = %s'); params.append(adjusted)
    if missing is not None:
        sets.append('"missingQuantity" = %s'); params.append(missing)
    cur.execute('UPDATE domae_order_batches SET ' + ', '.join(sets) + ' WHERE id = %s',
                (*params, batch_id))
    return ok, ng, unconfirmed


def _fail_pending_rows(cur, batch_id, message):
    """배치를 조기 실패시킬 때 서버가 만든 pending 주문행도 함께 마감한다.

    배치만 failed 로 바꾸면 품목 이력이 success=null, message='pending' 으로 영원히 남는다.
    """
    cur.execute(
        'UPDATE domae_cloud_orders SET success = false, "confirmedQuantity" = NULL, message = %s '
        'WHERE "batchId" = %s AND success IS NULL ' + _KEEP_UNCONFIRMED, (message, batch_id))


def _finalize_if_confirmed(conn, cur, batch_id, reason) -> bool:
    """확정된 주문이 이미 있으면 배치를 실제 행 기준으로 마감하고 True 를 돌려준다.

    단순히 completed 로 덮으면 successCount 등 집계가 하위 행과 어긋난다.
    """
    # 사전검증 실패(pre_fail)는 외부 전송이 일어났다는 증거가 아니다.
    # 이것까지 세면 락 재큐잉 후 정상 품목이 영구 취소된다.
    cur.execute('SELECT count(*) FROM domae_cloud_orders '
                'WHERE "batchId" = %s AND (success IS NOT NULL '
                'AND ("reasonCode" IS NULL OR "reasonCode" <> %s) '
                'OR success IS NULL AND "reasonCode" IN (\'send_unknown\', \'fallback_pending\'))', (batch_id, "pre_fail"))
    if not cur.fetchone()[0]:
        return False

    logger.error("batch 재실행 중단: batch=%s (%s)", batch_id, reason)
    cur.execute(
        'UPDATE domae_cloud_orders SET success = false, "confirmedQuantity" = NULL, message = %s '
        'WHERE "batchId" = %s AND success IS NULL ' + _KEEP_UNCONFIRMED, (reason, batch_id))
    cur.execute("""
        SELECT count(*) FILTER (WHERE success), count(*) FILTER (WHERE NOT success),
               count(*) FILTER (WHERE success IS NULL AND "reasonCode" IN ('send_unknown', 'fallback_pending')),
               coalesce(sum(CASE WHEN "reasonCode" = 'stock_adjusted' THEN 1 ELSE 0 END), 0),
               coalesce(sum(CASE WHEN "reasonCode" = 'stock_zero' THEN quantity
                                 WHEN "adjustedQuantity" IS NOT NULL
                                 THEN greatest(0, quantity - "adjustedQuantity")
                                 ELSE 0 END), 0)
        FROM domae_cloud_orders WHERE "batchId" = %s
    """, (batch_id,))
    ok, ng, unconfirmed, adj, missing = cur.fetchone()
    # 보호된 미확정 행은 사람이 확인할 때까지 processing 상태를 유지한다.
    # 확정 실패·완료 시각으로 표시하면 접수 불명을 확정 실패로 오인하게 된다.
    cur.execute("""
        UPDATE domae_order_batches
        SET status = %s, "completedAt" = CASE WHEN %s THEN NULL ELSE now() END, "successCount" = %s, "failCount" = %s,
            "adjustedCount" = %s, "missingQuantity" = %s
        WHERE id = %s
    """, ("processing" if unconfirmed else "completed" if ok else "failed", bool(unconfirmed), ok, ng, adj, missing, batch_id))
    conn.commit()
    return True


def _absorb_reconciled(cur, monitor_id, batch_id, supplier_name, rec, payload_items,
                       batch_items):
    """대조 결과를 주문 페이로드에 반영한다. **외부 전송 전에** 호출해야 한다.

    1) 웹에만 있던 품목을 배치에 편입하고 pending 주문행을 만든다.
       (전송 후에 만들면 그 사이 죽었을 때 주문 이력이 통째로 없다)
    2) 이미 있던 품목도 대조된 수량으로 다시 매핑한다. 복산은 웹 카트 전체를
       보내므로 payload 수량이 낡으면 이력과 실제 전송량이 어긋난다.
    반환: 추가된 품목 수
    """
    by_pid = {i.get("product_id"): i for i in payload_items}
    added = 0
    for w in rec.web_items:
        pid = w.get("product_id")
        if not pid:
            continue
        qty = int(w.get("quantity") or 0)
        if qty <= 0:
            # 파싱 이상이나 비정상 응답을 수량 1 로 둔갑시키면 안 된다.
            raise RuntimeError("대조 품목 수량 이상 (pc=%s qty=%r) — 주문 중단"
                               % (pid, w.get("quantity")))
        item = by_pid.get(pid)
        if item is None:
            item = {
                "product_id": pid,
                "quantity": qty,
                "product_name": w.get("product_name") or pid,
                "price": w.get("price"),
                "cart_item_id": w.get("cart_item_id"),
            }
            # 재실행 시 같은 배치에 이미 만들어둔 pending 행이 있으면 재사용한다.
            # 새로 INSERT 하면 워커 재실행마다 이력과 totalItems 가 부풀어 오른다.
            cur.execute(
                'SELECT id FROM domae_cloud_orders WHERE "batchId" = %s AND supplier = %s '
                'AND "productId" = %s AND success IS NULL',
                (batch_id, supplier_name, pid))
            _rows = cur.fetchall()
            if len(_rows) > 1:
                # 어느 행을 쓸지 정할 수 없다. 임의로 하나를 고르면 나머지가 영원히 pending 으로 남는다.
                raise RuntimeError(
                    "차집합 pending 주문행 중복 (batch=%s pc=%s %d건) — 주문 중단"
                    % (batch_id, pid, len(_rows)))
            _existing = _rows[0] if _rows else None
            if _existing:
                item["db_order_id"] = _existing[0]
                cur.execute('UPDATE domae_cloud_orders SET quantity = %s WHERE id = %s',
                            (qty, _existing[0]))
                payload_items.append(item)
                batch_items.append({"product_id": pid, "quantity": qty,
                                    "product_name": item["product_name"]})
                continue
            new_order_id = _generate_cuid()
            cur.execute("""
                INSERT INTO domae_cloud_orders
                (id, "monitorId", "batchId", supplier, "productName", quantity, price,
                 success, "confirmedQuantity", "productId", message, "orderedAt")
                VALUES (%s, %s, %s, %s, %s, %s, %s, NULL, NULL, %s, 'pending', now())
            """, (new_order_id, monitor_id, batch_id, supplier_name,
                  item["product_name"], qty, item.get("price"), pid))
            item["db_order_id"] = new_order_id
            payload_items.append(item)
            batch_items.append({"product_id": pid, "quantity": qty,
                                "product_name": item["product_name"]})
            added += 1
        else:
            # 수량·장바구니 id 재매핑
            if w.get("cart_item_id") and not item.get("cart_item_id"):
                item["cart_item_id"] = w["cart_item_id"]
            if int(item.get("quantity") or 0) != qty:
                item["quantity"] = qty
                if item.get("db_order_id"):
                    cur.execute(
                        'UPDATE domae_cloud_orders SET quantity = %s WHERE id = %s',
                        (qty, item["db_order_id"]))
                for bi in batch_items:
                    if bi.get("product_id") == pid:
                        bi["quantity"] = qty
    if added:
        cur.execute(
            'UPDATE domae_order_batches SET "totalItems" = "totalItems" + %s WHERE id = %s',
            (added, batch_id))
    return added


def _reject_unreconciled(supplier_name, crawler_cls, where):
    """대조·락을 거치지 않는 경로에서 cart-sync 공급사 주문을 막는다.

    복산처럼 전량 전송하는 도매상은 웹 카트에 있는 모든 품목이 함께 주문된다.
    대조 없이 order() 를 부르면 우리가 모르는 품목이 기록 없이 주문된다(설계 §1.2).
    이 경로들에 락·대조를 배선하기 전까지는 명시적으로 거부한다.
    반환: 거부 사유 문자열 또는 None
    """
    if getattr(crawler_cls, "SUPPORTS_CART_SYNC", False):
        msg = ("%s 은(는) 장바구니 전량 전송 방식이라 %s 경로에서 주문할 수 없습니다 "
               "— 장바구니 주문을 이용하세요" % (supplier_name, where))
        logger.warning("대조 미배선 경로 차단: supplier=%s where=%s", supplier_name, where)
        return msg
    return None


def _record_order_result(cur, monitor_id, batch_id, supplier_name, item, *,
                         success, message, order_id=None, adjusted_qty=None,
                         avail_stock=None, reason_code=None, confirmed_qty=None):
    """주문 결과 기록.

    서버가 미리 만든 pending 행(db_order_id)이 있으면 **UPDATE** 한다. 새로 INSERT 하면
    재큐잉·워커 재실행 때 실물 주문 1건에 DB 2행이 생긴다.
    대조가 추가한 차집합 품목만 db_order_id 가 없어 INSERT 로 떨어진다.
    """
    if reason_code == "send_unknown":
        success = None
    if confirmed_qty is None:
        confirmed_qty = confirmed_quantity(OrderResult(success=bool(success), adjusted_quantity=adjusted_qty,
                                           reason_code=reason_code), item.get("quantity", 1))
    utc_now = datetime.now(timezone.utc).replace(tzinfo=None)
    db_order_id = item.get("db_order_id")
    if db_order_id:
        cur.execute("""
            UPDATE domae_cloud_orders
            SET success = %s, message = %s, "orderId" = %s, "orderedAt" = %s,
                "adjustedQuantity" = %s, "availableStock" = %s, "confirmedQuantity" = %s, "reasonCode" = %s
            WHERE id = %s AND success IS NULL
        """, (success, message, order_id, utc_now,
              adjusted_qty, avail_stock, confirmed_qty, reason_code, db_order_id))
        if cur.rowcount:
            return
        # 이미 확정된 행이면 덮지 않는다 (재실행이 성공을 실패로 바꾸면 안 된다)
        cur.execute('SELECT success FROM domae_cloud_orders WHERE id = %s', (db_order_id,))
        _row = cur.fetchone()
        if _row is not None:
            logger.info("db_order_id=%s 는 이미 확정(success=%s) — 결과 기록 생략",
                        db_order_id, _row[0])
            return
        logger.warning("db_order_id=%s 행이 없어 신규 INSERT 로 폴백", db_order_id)

    cur.execute("""
        INSERT INTO domae_cloud_orders
        (id, "monitorId", "batchId", supplier, "productName", unit, "insuranceCode",
         quantity, price, success, "productId", "orderId", message, "orderedAt",
         "adjustedQuantity", "availableStock", "reasonCode", "confirmedQuantity")
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    """, (
        _generate_cuid(), monitor_id, batch_id, supplier_name,
        item.get("product_name", ""), item.get("unit"), item.get("insurance_code"),
        int(item.get("quantity", 1)), item.get("price"), success,
        item.get("product_id"), order_id, message, utc_now,
        adjusted_qty, avail_stock, reason_code, confirmed_qty,
    ))


class CloudScheduler:
    def __init__(self, db_pool, redis_client):
        self._db_pool = db_pool
        self._redis = redis_client
        self._crawlers = {}  # 캐시: {module_name: crawler_class}
        self._crawlers_loaded = False

    def _get_conn(self):
        """커넥션 풀에서 연결을 가져오고 SELECT 1로 유효성 검증.
        stale 커넥션이면 닫고 새로 가져온다."""
        conn = self._db_pool.getconn()
        try:
            cur = conn.cursor()
            cur.execute("SELECT 1")
            cur.close()
        except (psycopg2.OperationalError, psycopg2.InterfaceError):
            logger.warning("stale DB 커넥션 감지, 새 커넥션 획득")
            self._db_pool.putconn(conn, close=True)
            conn = self._db_pool.getconn()
        return conn

    @staticmethod
    def _decrypt_creds(raw_creds):
        """암호화된 credentials 복호화 (평문 폴백 제거)"""
        if isinstance(raw_creds, str):
            from domae_mcp.cloud.crypto import decrypt_credentials
            return decrypt_credentials(raw_creds)
        return raw_creds

    def execute(self, job: dict):
        """잡 1개 실행"""
        monitor_id = job["monitor_id"]
        conn = self._get_conn()
        try:
            # 1. 모니터 정보 조회
            cur = conn.cursor()
            cur.execute("""
                SELECT m.id, m.products, m.credentials,
                       m."telegramChatId", m."kakaoUserId",
                       k.tier
                FROM domae_cloud_monitors m
                JOIN domae_api_keys k ON m."apiKeyId" = k.id
                WHERE m.id = %s AND m."isActive" = true
            """, (monitor_id,))
            row = cur.fetchone()
            if not row:
                logger.warning("모니터 없음 또는 비활성: %s", monitor_id)
                return

            products = json.loads(row[1]) if isinstance(row[1], str) else row[1]
            raw_creds = row[2]
            credentials = self._decrypt_creds(raw_creds)
            telegram_chat_id = row[3]
            tier = row[5]

            # 2. 크롤러 로드 (최초 1회)
            if not self._crawlers_loaded:
                self._load_crawlers(conn)

            # 3. 도매별 병렬 검색 (도매당 1회 로그인 후 전 품목 순차 검색)
            target_suppliers = {
                name: (cls, credentials.get(name))
                for name, cls in self._crawlers.items()
                if credentials.get(name)
            }

            all_results = []
            if not target_suppliers:
                logger.warning("검색 대상 도매업체 없음 [%s] — 건너뜀", monitor_id)
                return

            # with 블록을 쓰지 않는다 — 종료 시 shutdown(wait=True) 가 멈춘 스레드를
            # 무한정 기다린다. 워커는 잡을 직렬 처리하므로 그동안 전 약국이 함께 멈춘다.
            # as_completed 에도 시한을 걸어야 한다. 시한 없이 두면 다음 완료를 기다리며
            # 블록되어 future.result(timeout=...) 까지 도달하지도 못한다.
            executor = ThreadPoolExecutor(max_workers=min(len(target_suppliers), 8))
            try:
                futures = {
                    executor.submit(
                        self._search_supplier, name, cls, cred, products
                    ): name
                    for name, (cls, cred) in target_suppliers.items()
                }
                try:
                    for future in as_completed(futures, timeout=SUPPLIER_SCAN_TIMEOUT):
                        supplier = futures[future]
                        try:
                            ret = future.result()
                            # _search_supplier는 하위 호환 위해 dict 반환
                            results = ret["results"] if isinstance(ret, dict) else ret
                            all_results.extend(results)
                        except Exception as e:
                            logger.error("도매 검색 실패 [%s]: %s", supplier, e)
                except FuturesTimeoutError:
                    stalled = [n for f, n in futures.items() if not f.done()]
                    logger.error(
                        "도매 검색 시한 초과 [%s] — 미완료 도매 %s (해당 도매 결과만 누락, 나머지는 반영)",
                        monitor_id, stalled,
                    )
            finally:
                executor.shutdown(wait=False, cancel_futures=True)

            # 4. 변동 감지 — 저장 BEFORE (prev baseline이 현재 save에 덮이지 않도록)
            # 전 도매 스캔 완료 후 제품별 합산 기준으로 drop 감지 (프론트 "재고모니터링"과 동일 기준)
            all_alerts = []
            for keyword in products:
                keyword_results = [r for r in all_results if r["keyword"] == keyword]
                alerts = self._detect_alerts(conn, monitor_id, keyword, keyword_results)
                all_alerts.extend(alerts)

            # 5. 결과 저장
            if all_results:
                self._save_results(conn, monitor_id, all_results)

            # 6. lastRunAt 업데이트
            utc_now = datetime.now(timezone.utc).replace(tzinfo=None)
            cur.execute(
                'UPDATE domae_cloud_monitors SET "lastRunAt" = %s, "updatedAt" = %s WHERE id = %s',
                (utc_now, utc_now, monitor_id)
            )
            conn.commit()

            if all_alerts and telegram_chat_id:
                from domae_mcp.cloud.notifier import Notifier
                for alert in all_alerts:
                    try:
                        if alert["type"] == "restock":
                            Notifier.send_restock_alert(
                                chat_id=telegram_chat_id,
                                monitor_id=monitor_id,
                                supplier=alert["supplier"],
                                product_name=alert["product_name"],
                                product_id=alert.get("product_id", ""),
                                quantity=alert["quantity"],
                                price=alert.get("price", 0),
                            )
                        elif alert["type"] == "drop":
                            Notifier.send_stock_drop_alert(
                                chat_id=telegram_chat_id,
                                supplier=alert["supplier"],
                                product_name=alert["product_name"],
                                old_qty=alert["old_qty"],
                                new_qty=alert["new_qty"],
                                price=alert.get("price", 0),
                            )
                        time.sleep(0.3)  # 텔레그램 rate limit 방지
                    except Exception as e:
                        logger.warning("알림 전송 실패: %s", e)

            logger.info("모니터 %s 완료: %d건 검색, %d건 알림", monitor_id, len(all_results), len(all_alerts))

            # 7. 활성 긴급주문 처리
            self._process_urgent_orders(conn, monitor_id, credentials)

        except Exception as e:
            conn.rollback()
            logger.error("모니터 실행 실패 [%s]: %s", monitor_id, e, exc_info=True)
        finally:
            # execute() 는 장바구니 락을 취득하지 않는다. 락 해제는 batch_order 전용이며
            # 여기에 두면 cart_locks 미정의로 NameError 가 나 putconn 이 실행되지 않는다
            # (2026-08-06 커넥션 풀 고갈 장애의 원인). finally 에는 반환만 남긴다.
            self._db_pool.putconn(conn)

    def _load_crawlers(self, conn):
        """DB에서 크롤러 코드 로드 (AES-GCM 복호화 → SHA-256 해시 검증 → 동적 import)

        저장 포맷:
        - 신규: 'v1:' prefix + base64(nonce+ciphertext+tag) — AES-256-GCM 암호화
        - 레거시: 평문 (마이그레이션 전 레코드)
        decrypt_crawler_code가 둘 다 처리.

        재진입 시 기존 캐시는 초기화 (DB에서 비활성화된 크롤러가 메모리에 잔존하는 것 방지).
        """
        self._crawlers.clear()

        cur = conn.cursor()
        cur.execute('SELECT name, code, "codeHash" FROM domae_crawlers WHERE "isActive" = true')
        rows = cur.fetchall()

        cache_dir = tempfile.mkdtemp(prefix="domae_cloud_")

        # base.py import 경로 확보
        # domae_mcp 패키지가 설치되어 있어야 함
        from domae_mcp.core.crawlers.base import BaseCrawler
        from domae_mcp.cloud.crypto import decrypt_crawler_code

        for name, stored_code, code_hash in rows:
            # 1) 복호화 (legacy 평문은 그대로 반환)
            try:
                plain_code = decrypt_crawler_code(stored_code)
            except Exception as e:
                logger.error("크롤러 [%s] 복호화 실패: %s", name, e)
                continue

            # 2) SHA-256 해시 검증 (평문 기준)
            if code_hash is None:
                logger.error("크롤러 [%s] 로드 거부: codeHash가 NULL입니다. 보안 정책에 의해 해시 없는 코드는 실행할 수 없습니다.", name)
                continue

            computed = hashlib.sha256(plain_code.encode("utf-8")).hexdigest()
            if computed != code_hash:
                logger.error(
                    "크롤러 [%s] 로드 거부: 코드 해시 불일치 (expected=%s, computed=%s)",
                    name, code_hash[:16], computed[:16],
                )
                continue

            try:
                file_path = os.path.join(cache_dir, f"{name}.py")
                with open(file_path, "w", encoding="utf-8") as f:
                    f.write(plain_code)

                spec = importlib.util.spec_from_file_location(f"domae_cloud.{name}", file_path)
                module = importlib.util.module_from_spec(spec)
                sys.modules[f"domae_cloud.{name}"] = module
                spec.loader.exec_module(module)

                for attr_name in dir(module):
                    attr = getattr(module, attr_name)
                    if isinstance(attr, type) and issubclass(attr, BaseCrawler) and attr is not BaseCrawler:
                        supplier_name = getattr(attr, "SUPPLIER_NAME", name)
                        self._crawlers[supplier_name] = attr
                        break

            except Exception as e:
                logger.error("크롤러 로드 실패 [%s]: %s", name, e)

        self._crawlers_loaded = True
        logger.info("크롤러 %d개 로드 완료 (암호화 복호화 포함)", len(self._crawlers))

    def _search_all(self, keyword: str, credentials: dict) -> list:
        """모든 도매상에서 검색"""
        results = []
        for supplier_name, crawler_cls in self._crawlers.items():
            cred = credentials.get(supplier_name)
            if not cred:
                continue

            try:
                crawler = crawler_cls()
                crawler.login(cred.get("login_id", ""), cred.get("login_pw", ""))
                search_results = crawler.search(keyword)

                for r in search_results:
                    results.append({
                        "keyword": keyword,
                        "supplier": supplier_name,
                        "product_name": r.product_name,
                        "unit": r.unit,
                        "insurance_code": getattr(r, "insurance_code", None),
                        "price": r.price,
                        "quantity": r.quantity,
                        "product_id": r.product_id,
                    })

                time.sleep(0.3)  # 도매사이트별 딜레이 (서로 다른 서버)

            except Exception as e:
                logger.warning("검색 실패 [%s/%s]: %s", supplier_name, keyword, e)

        return results

    def _search_supplier(self, supplier_name: str, crawler_cls, cred: dict, keywords: list) -> dict:
        """도매 1개에 대해 1회 로그인 후 전 품목 순차 검색.

        Returns:
            {
              "results": list,
              "login_ok": bool,             # 로그인 성공 여부 (False면 모든 키워드 무응답)
              "failed_keywords": set[str],  # 검색 실패한 키워드 (로그인 성공했지만 개별 키워드 에러)
            }
        """
        results = []
        failed_keywords: set = set()
        login_ok = True
        try:
            crawler = crawler_cls()
            crawler.login(cred.get("login_id", ""), cred.get("login_pw", ""))

            for keyword in keywords:
                try:
                    search_results = crawler.search(keyword)
                    for r in search_results:
                        results.append({
                            "keyword": keyword,
                            "supplier": supplier_name,
                            "product_name": r.product_name,
                            "unit": r.unit,
                            "insurance_code": getattr(r, "insurance_code", None),
                            "price": r.price,
                            "quantity": r.quantity,
                            "product_id": r.product_id,
                        })
                except Exception as e:
                    failed_keywords.add(keyword)
                    logger.warning("검색 실패 [%s/%s]: %s", supplier_name, keyword, e)
                time.sleep(0.5)  # 같은 사이트 내 품목 간 딜레이

        except Exception as e:
            login_ok = False
            logger.error("도매 로그인 실패 [%s]: %s", supplier_name, e)

        return {"results": results, "login_ok": login_ok, "failed_keywords": failed_keywords}

    def _save_results(self, conn, monitor_id: str, results: list):
        """검색 결과 DB 저장 (스냅샷 누적 + 24h 정리).

        INVARIANT (불변조건):
            단일 호출 내 모든 row는 동일한 `utc_now`(scannedAt/searchedAt)를 공유해야 한다.
            프론트 재고 히스토리 차트(cloud.ts GET /cloud/products/:keyword/history)가
            "같은 scannedAt = 같은 스캔 싸이클"로 가정하고 시점별 supplier 합산을 수행하므로,
            supplier별로 타임스탬프가 달라지면 차트가 per-supplier 값만 그려 과소 표시된다.
            향후 per-supplier 타임스탬프 도입 시 차트 쿼리도 함께 수정해야 함.
        """
        cur = conn.cursor()
        utc_now = datetime.now(timezone.utc).replace(tzinfo=None)  # 반드시 1회만 생성

        # 24시간 초과 스냅샷을 일별 평균으로 압축 보존
        self._compact_old_snapshots(cur, monitor_id)

        for r in results:
            cur.execute("""
                INSERT INTO domae_cloud_results
                (id, "monitorId", keyword, supplier, "productName", unit, "insuranceCode", price, quantity, "productId", "searchedAt")
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (
                _generate_cuid(), monitor_id, r["keyword"], r["supplier"], r["product_name"],
                r.get("unit"), r.get("insurance_code"), r.get("price"), r.get("quantity"), r.get("product_id"), utc_now,
            ))
            # 스냅샷 누적 저장 (교체 아닌 INSERT)
            cur.execute("""
                INSERT INTO domae_inventory_snapshots
                (id, "monitorId", supplier, "productName", unit, "insuranceCode", quantity, price, "productId", "scannedAt")
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (
                _generate_cuid(), monitor_id, r["supplier"], r["product_name"],
                r.get("unit"), r.get("insurance_code"), r.get("quantity"), r.get("price"),
                r.get("product_id"), utc_now,
            ))

    def _compact_old_snapshots(self, cur, monitor_id: str):
        """스냅샷 단계별 압축:
        - 3일 이내: 원본 유지 (30분 간격)
        - 3~7일: 12시간 평균 (1일 2건)
        - 7~90일: 1일 평균 (1일 1건)
        - 90일 초과: 삭제
        """
        try:
            # 1단계: 3~7일 데이터 → 12시간 평균으로 압축
            cur.execute("""
                SELECT "monitorId", supplier, "productName", unit, "insuranceCode", "productId",
                       DATE("scannedAt") as snap_date,
                       CASE WHEN EXTRACT(HOUR FROM "scannedAt") < 12 THEN 0 ELSE 12 END as half,
                       AVG(COALESCE(quantity, 0))::int as avg_qty,
                       AVG(COALESCE(price, 0))::int as avg_price
                FROM domae_inventory_snapshots
                WHERE "monitorId" = %s
                  AND "scannedAt" < NOW() - INTERVAL '3 days'
                  AND "scannedAt" >= NOW() - INTERVAL '7 days'
                GROUP BY "monitorId", supplier, "productName", unit, "insuranceCode", "productId",
                         DATE("scannedAt"),
                         CASE WHEN EXTRACT(HOUR FROM "scannedAt") < 12 THEN 0 ELSE 12 END
                HAVING COUNT(*) > 1
            """, (monitor_id,))
            half_groups = cur.fetchall()

            if half_groups:
                cur.execute("""
                    DELETE FROM domae_inventory_snapshots
                    WHERE "monitorId" = %s
                      AND "scannedAt" < NOW() - INTERVAL '3 days'
                      AND "scannedAt" >= NOW() - INTERVAL '7 days'
                """, (monitor_id,))

                for row in half_groups:
                    mid, supplier, product_name, unit_val, ins_code, product_id, snap_date, half, avg_qty, avg_price = row
                    compacted_time = datetime.combine(snap_date, datetime.min.time().replace(hour=int(half)))
                    cur.execute("""
                        INSERT INTO domae_inventory_snapshots
                        (id, "monitorId", supplier, "productName", unit, "insuranceCode",
                         quantity, price, "productId", "scannedAt")
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """, (
                        _generate_cuid(), mid, supplier, product_name, unit_val, ins_code,
                        avg_qty, avg_price, product_id, compacted_time,
                    ))

                logger.info("스냅샷 12h 압축 [%s]: %d개 그룹", monitor_id, len(half_groups))

            # 2단계: 7~90일 데이터 → 1일 평균으로 압축
            cur.execute("""
                SELECT "monitorId", supplier, "productName", unit, "insuranceCode", "productId",
                       DATE("scannedAt") as snap_date,
                       AVG(COALESCE(quantity, 0))::int as avg_qty,
                       AVG(COALESCE(price, 0))::int as avg_price
                FROM domae_inventory_snapshots
                WHERE "monitorId" = %s
                  AND "scannedAt" < NOW() - INTERVAL '7 days'
                  AND "scannedAt" >= NOW() - INTERVAL '90 days'
                GROUP BY "monitorId", supplier, "productName", unit, "insuranceCode", "productId",
                         DATE("scannedAt")
                HAVING COUNT(*) > 1
            """, (monitor_id,))
            day_groups = cur.fetchall()

            if day_groups:
                cur.execute("""
                    DELETE FROM domae_inventory_snapshots
                    WHERE "monitorId" = %s
                      AND "scannedAt" < NOW() - INTERVAL '7 days'
                      AND "scannedAt" >= NOW() - INTERVAL '90 days'
                """, (monitor_id,))

                for row in day_groups:
                    mid, supplier, product_name, unit_val, ins_code, product_id, snap_date, avg_qty, avg_price = row
                    compacted_time = datetime.combine(snap_date, datetime.min.time().replace(hour=12))
                    cur.execute("""
                        INSERT INTO domae_inventory_snapshots
                        (id, "monitorId", supplier, "productName", unit, "insuranceCode",
                         quantity, price, "productId", "scannedAt")
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """, (
                        _generate_cuid(), mid, supplier, product_name, unit_val, ins_code,
                        avg_qty, avg_price, product_id, compacted_time,
                    ))

                logger.info("스냅샷 일별 압축 [%s]: %d개 그룹", monitor_id, len(day_groups))

            # 3단계: 90일 초과 삭제
            cur.execute("""
                DELETE FROM domae_inventory_snapshots
                WHERE "monitorId" = %s AND "scannedAt" < NOW() - INTERVAL '90 days'
            """, (monitor_id,))

        except Exception as e:
            logger.warning("스냅샷 압축 실패 [%s]: %s", monitor_id, e)

    def _detect_alerts(
        self,
        conn,
        monitor_id: str,
        keyword: str,
        new_results: list,
    ) -> list:
        """키워드(모니터 등록 단위) 합산 기준 알림 감지.

        정책:
            하나의 모니터 등록(keyword = 보험코드/약품명)에서 나온 모든 검색결과
            (도매·제품명·포장단위 무관)의 quantity 를 합산하여 **단일 수량** 으로 취급한다.
            따라서 per-supplier 또는 per-product_name 기준이 아니라 오직 전체 합산값의
            상태 변화만 감지한다.

        감지 이벤트:
            1. 재입고: 합산 0 → 양수 전환 시 1회 (전 도매 품절 해제)
            2. 재고 급감: 합산이 직전 cycle 대비 30% 이상 감소

        첫 cycle(prev 없음)은 비교 기준이 없으므로 모두 skip.

        주문 버튼 컨텍스트용 대표 row 는 새 cycle 에서 재고가 가장 많은 도매를 선택.

        Note:
            크롤러 일부가 이번 cycle 에 실패하면 new_total 이 일시적으로 낮아져
            false drop 또는 false restock 이 발생할 수 있음. 합산 일관성을 우선함.
        """
        cur = conn.cursor()

        # 현재 키워드의 제품명 집합
        product_names = list({r["product_name"] for r in new_results})
        if not product_names:
            # new_results 비어있음 = 전 도매 empty 응답(true-stockout) 또는 전 도매 실패
            # 과거 기록에서 이 키워드와 연결된 product_name 복원
            cur.execute("""
                SELECT DISTINCT "productName"
                FROM domae_cloud_results
                WHERE "monitorId" = %s AND keyword = %s
            """, (monitor_id, keyword))
            product_names = [row[0] for row in cur.fetchall() if row[0]]

        if not product_names:
            return []  # 비교 불가

        # 이전 cycle의 searchedAt batch 합산 (프론트 /cloud/results/summary와 동일 스코프)
        # - domae_cloud_results 사용: keyword 컬럼이 있어 cross-keyword 오염 차단
        # - _save_results가 domae_inventory_snapshots와 동일 utc_now로 INSERT하므로 값 동일
        # - _detect_alerts가 _save_results BEFORE 실행 → MAX(searchedAt) = 직전 cycle
        cur.execute("""
            SELECT "productName", SUM(COALESCE(quantity, 0))::int
            FROM domae_cloud_results
            WHERE "monitorId" = %s
              AND keyword = %s
              AND "searchedAt" = (
                  SELECT MAX("searchedAt") FROM domae_cloud_results
                  WHERE "monitorId" = %s AND keyword = %s
              )
            GROUP BY "productName"
        """, (monitor_id, keyword, monitor_id, keyword))
        prev_totals: dict = {row[0]: row[1] for row in cur.fetchall()}

        if not prev_totals:
            return []  # 첫 cycle — 비교 기준 없음

        # 현재 cycle 합산 (프론트 summary와 동일 기준, per product_name)
        new_totals: dict = {}
        for r in new_results:
            pname = r["product_name"]
            qty = r.get("quantity") or 0
            new_totals[pname] = new_totals.get(pname, 0) + qty

        # 키워드(전 도매·전 제품명) 합산 — 사용자 정책: 등록 1건 = 수량 1개
        old_keyword_total = sum(prev_totals.values())
        new_keyword_total = sum(new_totals.values())

        # 대표 row: 새 cycle 에서 재고가 가장 많은 도매 (주문 버튼용)
        positive_rows = [r for r in new_results if (r.get("quantity") or 0) > 0]
        top_row = max(
            positive_rows,
            key=lambda r: r.get("quantity") or 0,
            default=None,
        )

        # 대표 표시 약명: 도매마다 "코싹엘정", "한미 코싹엘정 120mg/30T",
        # "코싹엘정120mg(병) 30T 한미" 처럼 뒤에 용량·포장·T수·(병)·제약사가
        # 붙은 변형이 섞여 있다. 뒤꼬리(용량 단위 \d+mg/ml/g/T 이후, 또는 괄호)
        # 를 잘라 공통 접두(약명 본체)만 남긴 뒤 가장 짧은 이름을 채택한다.
        # keyword 가 보험코드(숫자)인 경우에도 실제 약명이 메시지에 노출된다.
        cleaned_names: set = set()
        for r in new_results:
            nm = r.get("product_name") or ""
            # 용량·포장 뒤꼬리 제거: "...정120mg(병) 30T 한미" / "...정 120mg/30T" → 본체만
            nm = re.sub(r'\s*\d+\s*(mg|ml|g|T|t)\b.*$', '', nm, flags=re.IGNORECASE)
            # 뒤에 남은 괄호 블록 + 이후 꼬리 제거: "...정 (병) ..." → "...정"
            nm = re.sub(r'\s*\(.*?\).*$', '', nm)
            nm = nm.strip()
            if nm:
                cleaned_names.add(nm)

        rep_supplier = top_row["supplier"] if top_row else "전체"
        rep_product_name = min(cleaned_names, key=len) if cleaned_names else keyword
        rep_product_id = (top_row.get("product_id") if top_row else "") or ""
        rep_price = (top_row.get("price") if top_row else 0) or 0

        alerts = []

        # 1. 재입고: 전 도매 합산 0 → 양수 전환 시 1회
        if old_keyword_total == 0 and new_keyword_total > 0:
            alerts.append({
                "type": "restock",
                "supplier": rep_supplier,
                "product_name": rep_product_name,
                "product_id": rep_product_id,
                "quantity": new_keyword_total,
                "price": rep_price,
            })

        # 2. 재고 급감: 전 도매 합산 30% 이상 감소
        if old_keyword_total >= 10 and new_keyword_total < old_keyword_total:
            drop_pct = (old_keyword_total - new_keyword_total) / old_keyword_total
            if drop_pct >= 0.3:
                alerts.append({
                    "type": "drop",
                    "supplier": "전체",
                    "product_name": rep_product_name,
                    "product_id": rep_product_id,
                    "old_qty": old_keyword_total,
                    "new_qty": new_keyword_total,
                    "price": rep_price,
                })

        return alerts

    def search_on_demand(self, job: dict):
        """온디맨드 검색 — 도매상별로 stream_key에 결과를 실시간 전송"""
        monitor_id = job["monitor_id"]
        stream_key = job["stream_key"]
        keywords = job.get("keywords", [])
        requested_suppliers = job.get("suppliers", [])

        # 중복 검색 방지 락 (서버 dedup이 빠지거나 다른 경로로 들어온 중복 잡 차단)
        # 서버 락(`domae:search:lock:...`)과 충돌하지 않도록 별도 prefix 사용
        # TTL 180s — 도매상 1곳 최대 120s + 60s 버퍼
        dedup_payload = json.dumps({
            "m": monitor_id,
            "k": sorted([str(k).strip() for k in keywords]),
            "s": sorted([str(s) for s in requested_suppliers]),
        }, sort_keys=True, ensure_ascii=False)
        dedup_hash = hashlib.sha1(dedup_payload.encode("utf-8")).hexdigest()
        worker_lock_key = f"domae:search:worker_lock:{monitor_id}:{dedup_hash}"
        # Redis 일시 장애 시 fail-open: set이 throw하면 락 없이 진행 (worker.py가 exception을 잡아줌)
        try:
            lock_ok = self._redis.set(worker_lock_key, "1", nx=True, ex=180)
        except Exception as e:
            logger.warning("dedup 락 set 실패 (fail-open으로 진행): %s", e)
            lock_ok = True
            worker_lock_key = None  # finally에서 delete 안 하도록
        if not lock_ok:
            logger.warning(
                "search_on_demand 중복 잡 차단: monitor=%s keywords=%s",
                monitor_id, keywords,
            )
            try:
                self._redis.lpush(stream_key, json.dumps({"type": "done"}))
            except Exception:
                pass
            return

        conn = self._get_conn()
        try:
            # 1. 모니터 정보 조회 (credentials 가져오기)
            cur = conn.cursor()
            cur.execute("""
                SELECT m.credentials
                FROM domae_cloud_monitors m
                WHERE m.id = %s AND m."isActive" = true
            """, (monitor_id,))
            row = cur.fetchone()
            if not row:
                self._redis.lpush(stream_key, json.dumps({"type": "error", "message": "모니터 없음 또는 비활성"}))
                self._redis.lpush(stream_key, json.dumps({"type": "done"}))
                return

            raw_creds = row[0]
            credentials = self._decrypt_creds(raw_creds)

            # 2. 크롤러 로드
            if not self._crawlers_loaded:
                self._load_crawlers(conn)

            # 3. 대상 도매상 결정 (requested_suppliers 순서 보장)
            target_suppliers = {}
            if requested_suppliers:
                for supplier_name in requested_suppliers:
                    crawler_cls = self._crawlers.get(supplier_name)
                    cred = credentials.get(supplier_name)
                    if crawler_cls and cred:
                        target_suppliers[supplier_name] = (crawler_cls, cred)
            else:
                for supplier_name, crawler_cls in self._crawlers.items():
                    cred = credentials.get(supplier_name)
                    if cred:
                        target_suppliers[supplier_name] = (crawler_cls, cred)

            # 4. 도매상별 병렬 검색 + 완료 시 즉시 stream 전송
            def _search_and_stream(supplier_name, crawler_cls, cred):
                try:
                    supplier_payload = self._search_supplier(supplier_name, crawler_cls, cred, keywords)
                    self._redis.lpush(stream_key, json.dumps({
                        "type": "partial",
                        "supplier": supplier_name,
                        "results": supplier_payload["results"],
                        "login_ok": supplier_payload["login_ok"],
                        "failed_keywords": list(supplier_payload["failed_keywords"]),
                    }))
                except Exception as e:
                    logger.warning("도매상 검색 실패 [%s]: %s", supplier_name, e)
                    self._redis.lpush(stream_key, json.dumps({
                        "type": "partial",
                        "supplier": supplier_name,
                        "results": [],
                        "error": str(e),
                    }))

            # with 미사용 이유는 execute() 와 동일 — 멈춘 스레드를 기다리면 워커 전체가 멈춘다.
            executor = ThreadPoolExecutor(max_workers=min(len(target_suppliers), 8))
            try:
                futures = [
                    executor.submit(_search_and_stream, name, cls, cred)
                    for name, (cls, cred) in target_suppliers.items()
                ]
                try:
                    for future in as_completed(futures, timeout=SUPPLIER_SCAN_TIMEOUT):
                        try:
                            future.result()
                        except Exception as e:
                            logger.error("search_on_demand 스레드 에러: %s", e)
                except FuturesTimeoutError:
                    logger.error(
                        "search_on_demand 시한 초과 [%s] — 미완료 %d건",
                        monitor_id, sum(1 for f in futures if not f.done()),
                    )
            finally:
                executor.shutdown(wait=False, cancel_futures=True)

            # 5. 전체 완료
            self._redis.lpush(stream_key, json.dumps({"type": "done"}))
            logger.info("search_on_demand 완료: monitor=%s, %d개 도매상", monitor_id, len(target_suppliers))

        except Exception as e:
            logger.error("search_on_demand 실패 [%s]: %s", monitor_id, e, exc_info=True)
            try:
                self._redis.lpush(stream_key, json.dumps({"type": "error", "message": str(e)}))
                self._redis.lpush(stream_key, json.dumps({"type": "done"}))
            except Exception:
                pass
        finally:
            self._db_pool.putconn(conn)
            # dedup 락 해제 (TTL이 안전망). fail-open으로 None이 들어왔을 수 있음.
            if worker_lock_key:
                try:
                    self._redis.delete(worker_lock_key)
                except Exception:
                    pass

    def order(self, job: dict):
        """단건 주문 실행 — DB finalize 먼저 + response_key 로 결과 반환"""
        monitor_id = job["monitor_id"]
        response_key = job["response_key"]
        response_key_ttl = int(job.get("response_key_ttl", 180))
        supplier_name = job["supplier"]
        product_id = job["product_id"]
        quantity = job["quantity"]
        db_order_id = job.get("db_order_id")  # quick-order가 전달한 pending 레코드 ID
        db_batch_id = job.get("db_batch_id")  # quick-order의 단건 batch ID (주문이력 노출용)

        # 결과를 DB 먼저 + response_key 나중에 반영하는 헬퍼
        # — 순서 중요: DB가 먼저 확정돼야 서버 timeout 후에도 최종 상태가 정확함
        # — 모든 내부 예외는 이 함수 안에서 삼키고 절대 밖으로 던지지 않음 (재귀 finalize 방지)
        def _finalize(success: bool | None, order_id: str | None, message: str,
                      adjusted_quantity: int | None = None,
                      available_stock: int | None = None,
                      reason_code: str | None = None, confirmed_qty: int | None = None):
            if reason_code == "send_unknown":
                success = None
            elif success is False and not may_have_sent:
                reason_code = reason_code or "not_sent"
            # 1) DB UPDATE 먼저 (quick-order 경로만)
            if db_order_id:
                try:
                    upd_conn = self._get_conn()
                    try:
                        upd_cur = upd_conn.cursor()
                        upd_cur.execute("""
                            UPDATE domae_cloud_orders
                            SET success = %s,
                                "orderId" = %s,
                                message = %s,
                                "adjustedQuantity" = %s,
                                "availableStock" = %s,
                                "reasonCode" = %s, "confirmedQuantity" = %s
                            WHERE id = %s AND success IS NULL
                        """, (success, order_id, message,
                              adjusted_quantity, available_stock, reason_code,
                              confirmed_qty if confirmed_qty is not None else confirmed_quantity(OrderResult(success=bool(success), adjusted_quantity=adjusted_quantity, reason_code=reason_code), quantity), db_order_id))
                        # quick-order 단건 batch도 함께 마감
                        if db_batch_id:
                            utc_now = datetime.now(timezone.utc).replace(tzinfo=None)
                            # 수량 조정 집계 계산
                            _adj_count = 1 if reason_code == "stock_adjusted" else 0
                            _missing = 0
                            try:
                                if reason_code == "stock_adjusted" and adjusted_quantity is not None:
                                    _missing = max(0, int(quantity) - int(adjusted_quantity))
                                elif reason_code == "stock_zero":
                                    _missing = int(quantity)
                            except Exception:
                                pass
                            upd_cur.execute("""
                                UPDATE domae_order_batches
                                SET status = %s,
                                    "successCount" = %s,
                                    "failCount" = %s,
                                    "adjustedCount" = %s,
                                    "missingQuantity" = %s,
                                    "completedAt" = %s
                                WHERE id = %s
                            """, ("processing" if success is None else "completed", 1 if success else 0, 1 if success is False else 0,
                                  _adj_count, _missing, None if success is None else utc_now, db_batch_id))
                        if success is None and job.get("cart_item_id"):
                            upd_cur.execute('UPDATE domae_cart_items SET "failedAt" = %s, "failReason" = %s WHERE id = %s',
                                            (datetime.now(timezone.utc).replace(tzinfo=None),
                                             "전송 결과 확인 필요 — 도매몰 주문내역을 확인하세요", job["cart_item_id"]))
                        upd_conn.commit()
                        logger.info("order DB finalize: dbOrderId=%s dbBatchId=%s success=%s reason=%s adj=%s",
                                    db_order_id, db_batch_id, success, reason_code, adjusted_quantity)
                    except Exception:
                        upd_conn.rollback()
                        raise
                    finally:
                        try:
                            self._db_pool.putconn(upd_conn)
                        except Exception:
                            pass
                except Exception as e:
                    logger.error("order DB finalize 실패 [dbOrderId=%s]: %s", db_order_id, e, exc_info=True)
                    if may_have_sent:
                        success, reason_code = None, "send_unknown"
                        message = "결과 기록 실패 — 도매몰 주문내역 확인 필요"

            # 2) response_key lpush + TTL 설정 (서버 BRPOP용)
            payload = {
                "success": success, "order_id": order_id, "message": message,
                "adjusted_quantity": adjusted_quantity,
                "available_stock": available_stock,
                "reason_code": reason_code,
            }
            try:
                self._redis.lpush(response_key, json.dumps(payload))
                # TTL은 lpush 직후에 설정해야 key가 존재해서 적용됨
                # 서버가 BRPOP으로 즉시 consume하면 key 자동 삭제, timeout 시엔 TTL로 정리
                self._redis.expire(response_key, response_key_ttl)
            except Exception as e:
                logger.warning("order response_key lpush/expire 실패: %s", e)
            return payload

        conn = self._get_conn()
        may_have_sent = False
        try:
            # 1. credentials + telegramChatId 조회
            cur = conn.cursor()
            if db_batch_id:
                cur.execute('UPDATE domae_order_batches SET status = \'processing\' '
                            'WHERE id = %s AND "monitorId" = %s AND status = \'pending\' RETURNING id',
                            (db_batch_id, monitor_id))
                if cur.fetchone() is None:
                    conn.rollback()
                    logger.warning("order 재실행 중단: batch=%s 소유권 없음", db_batch_id)
                    return
                conn.commit()
                if _finalize_if_confirmed(conn, cur, db_batch_id, "단건 주문 재실행 중단"):
                    return
            elif db_order_id:
                cur.execute('SELECT success, "reasonCode" FROM domae_cloud_orders WHERE id = %s', (db_order_id,))
                existing = cur.fetchone()
                if existing is None or existing[0] is not None or existing[1] in UNCONFIRMED_REASONS:
                    conn.rollback()
                    return
            cur.execute("""
                SELECT m.credentials, m."telegramChatId"
                FROM domae_cloud_monitors m
                WHERE m.id = %s AND m."isActive" = true
            """, (monitor_id,))
            row = cur.fetchone()
            if not row:
                _finalize(False, None, "모니터 없음")
                return

            raw_creds = row[0]
            credentials = self._decrypt_creds(raw_creds)
            telegram_chat_id = row[1]

            # 2. 크롤러 로드
            if not self._crawlers_loaded:
                self._load_crawlers(conn)

            cred = credentials.get(supplier_name)
            if not cred:
                _finalize(False, None, f"{supplier_name} 계정 미등록")
                return

            crawler_cls = self._crawlers.get(supplier_name)
            if not crawler_cls:
                _finalize(False, None, f"{supplier_name} 크롤러 없음")
                return

            # 3. 주문 실행
            crawler = crawler_cls()
            crawler.cart_snapshot = CartSnapshot(self._redis, monitor_id, supplier_name, account=cred.get("login_id", ""))
            if not crawler.login(cred.get("login_id", ""), cred.get("login_pw", "")):
                raise CrawlerError("로그인 실패")
            # 주문마다 새 크롤러 인스턴스라 토큰/단가 캐시가 비어 있다.
            # job 에 싣려온 product_name 으로 선행 search 를 1회 수행해
            # 캐시를 채운다. search 결과는 버리며 캐시를 쓰는 크롤러(TJ팜 등)만
            # 영향을 받는다.
            product_name_hint = job.get("product_name", "") or ""
            if product_name_hint:
                try:
                    crawler.search(product_name_hint)
                except Exception as e:
                    logger.warning(
                        "order 전 선행 search 실패 [%s / %s]: %s",
                        supplier_name, product_name_hint, e,
                    )
            # product_name 을 crawler.order 에 전달 → Phase 2 fallback 의 refetch_stock 이 search 기반 폴백 사용 가능
            # 단건 주문도 대조·전송이 같은 락 안에서 이뤄져야 한다. 락이 없으면
            # cart_sync 가 끼어들어 카트가 바뀐 채로 전송될 수 있다.
            single_token = None
            if getattr(crawler_cls, "SUPPORTS_CART_SYNC", False):
                single_token = _acquire_cart_lock(self._redis, monitor_id, supplier_name)
                if not single_token:
                    _finalize(success=False, order_id=None,
                              message=f"{supplier_name} 장바구니 락 획득 실패 — 주문 중단")
                    return
            try:
                if single_token:
                    # 단건은 재담기를 끈다. 켜두면 요청하지 않은 품목이 웹 카트에 들어가
                    # 전량 전송에 휩쓸린다(대조가 바로주문을 깨는 회귀).
                    rec = reconcile_cart(
                        conn, self._redis, monitor_id, supplier_name, crawler,
                        in_flight_product_id=product_id, restore=False,
                        exclude_batch_id=db_batch_id,
                        renew_cb=lambda: _renew_cart_lock(
                            self._redis, monitor_id, supplier_name, single_token))
                    if rec.fatal:
                        conn.rollback()
                        _finalize(success=False, order_id=None,
                                  message=f"대조 중단: {rec.fatal}")
                        return
                    conn.commit()   # 외부 전송 전 durable
                    # in_flight 로 요청 제품을 뺐으므로 web_items 는 곧 "요청 외 품목" 이다.
                    # 복산은 전량 전송이라 이게 남아 있으면 기록 없는 동반 주문이 된다.
                    if rec.web_items:
                        _finalize(success=False, order_id=None,
                                  message="장바구니에 요청 외 품목 %s 존재 — 주문 중단"
                                          % sorted(w["product_id"] for w in rec.web_items))
                        return
                    if not _renew_cart_lock(self._redis, monitor_id, supplier_name, single_token):
                        _finalize(success=False, order_id=None,
                                  message="전송 직전 락 상실 — 주문 중단")
                        return
                may_have_sent = True  # 커밋 응답 유실도 보수적으로 미확정 처리한다.
                if db_batch_id:
                    _mark_sending(conn, cur, db_batch_id, supplier_name)
                elif db_order_id:
                    cur.execute('UPDATE domae_cloud_orders SET "reasonCode" = %s, message = %s '
                                'WHERE id = %s AND success IS NULL ' + _KEEP_UNCONFIRMED,
                                ('send_unknown', '전송 결과 확인 필요 — 주문 실행 중', db_order_id))
                    if not cur.rowcount:
                        conn.rollback()
                        return
                    conn.commit()
                result = crawler.order(product_id, quantity, product_name=product_name_hint,
                                       insurance_code=job.get("insurance_code"))
            finally:
                _release_cart_lock(self._redis, monitor_id, supplier_name, single_token)

            finalized = _finalize(
                success=_db_success(result),
                order_id=getattr(result, "order_id", None),
                message=getattr(result, "message", "") or "",
                adjusted_quantity=getattr(result, "adjusted_quantity", None),
                available_stock=getattr(result, "available_stock", None),
                reason_code=getattr(result, "reason_code", None),
                confirmed_qty=confirmed_quantity(result, quantity),
            )

            # 텔레그램 알림
            if telegram_chat_id:
                try:
                    from domae_mcp.cloud.notifier import Notifier
                    product_name = job.get("product_name", product_id)
                    msg = _quick_order_message(supplier_name, product_name, int(quantity), result,
                                               finalized["success"])
                    Notifier.send_telegram(telegram_chat_id, msg)
                except Exception as e:
                    logger.warning("주문 텔레그램 알림 실패: %s", e)

            logger.info("order 완료: monitor=%s supplier=%s success=%s", monitor_id, supplier_name, finalized["success"])

        except Exception as e:
            logger.error("order 실패 [%s]: %s", monitor_id, e, exc_info=True)
            conn.rollback()
            _finalize(None if may_have_sent else False, None, str(e), reason_code="send_unknown" if may_have_sent else "not_sent")
        finally:
            self._db_pool.putconn(conn)

    def batch_order(self, job: dict):
        """일괄 주문 — DB에 직접 결과 기록 (비동기 배치)"""
        monitor_id = job["monitor_id"]
        batch_id = job["batch_id"]
        items = job.get("items", [])

        conn = self._get_conn()
        cart_locks = {}   # 바깥 finally 에서 해제하려면 try 진입 전에 있어야 한다
        try:
            cur = conn.cursor()

            # 1. 배치 소유권 **원자적** 획득.
            # SELECT 로 확인하고 UPDATE 하면 동시 실행 둘 다 통과해 중복 주문이 된다.
            utc_now = datetime.now(timezone.utc).replace(tzinfo=None)
            cur.execute("""
                UPDATE domae_order_batches SET status = 'processing'
                WHERE id = %s AND "monitorId" = %s AND status = 'pending'
                RETURNING id
            """, (batch_id, monitor_id))
            if cur.fetchone() is None:
                conn.rollback()
                logger.warning("batch_order 중단: batch=%s 소유권 획득 실패 "
                               "(이미 처리 중이거나 마감됨)", batch_id)
                return
            conn.commit()

            # 2. 멱등 게이트 — 이미 확정된 주문이 있으면 외부 전송을 반복하면 안 된다.
            if _finalize_if_confirmed(conn, cur, batch_id, "이미 확정된 배치 — 재실행 중단"):
                return

            # 2. credentials 조회
            cur.execute("""
                SELECT m.credentials, m."telegramChatId"
                FROM domae_cloud_monitors m
                WHERE m.id = %s
            """, (monitor_id,))
            row = cur.fetchone()
            if not row:
                _fail_pending_rows(cur, batch_id, "모니터 없음")
                cur.execute(
                    'UPDATE domae_order_batches SET status = %s, "completedAt" = now(), '
                    '"failCount" = (SELECT count(*) FROM domae_cloud_orders WHERE "batchId" = %s) '
                    'WHERE id = %s',
                    ("failed", batch_id, batch_id)
                )
                conn.commit()
                return

            raw_creds = row[0]
            credentials = self._decrypt_creds(raw_creds)

            telegram_chat_id = row[1]

            # 3. 크롤러 로드
            if not self._crawlers_loaded:
                self._load_crawlers(conn)

            # 4. 도매상별 그룹핑 → 일괄 주문
            success_count = 0
            fail_count = 0
            adjusted_count = 0          # 수량 자동 조정된 품목 수 (부분 재고)
            missing_qty_total = 0       # 요청 - 실제 주문된 총 수량 (누락 수량)
            success_lines = []          # 텔레그램 알림용 — 정상 성공
            adjusted_lines = []         # 수량 조정 성공 (⚠️ 섹션)
            missing_lines = []          # 재고 0 누락 (❌ 섹션)
            fail_lines = []             # 기타 실패
            unconfirmed_lines = []      # 전송 결과 확인 필요 (실패 아님)
            logged_in_crawlers = {}     # 도매상별 로그인 캐시

            # 4-1. 사전 검증 + 도매상별 그룹핑
            from collections import OrderedDict
            supplier_groups = OrderedDict()  # {supplier: [(idx, item), ...]}
            pre_fail = {}  # {idx: error_message}

            for idx, item in enumerate(items):
                supplier_name = item.get("supplier")
                if not supplier_name:
                    pre_fail[idx] = "도매상 미지정"
                    continue
                cred = credentials.get(supplier_name)
                if not cred:
                    pre_fail[idx] = f"{supplier_name} 계정 미등록"
                    continue
                crawler_cls = self._crawlers.get(supplier_name)
                if not crawler_cls:
                    pre_fail[idx] = f"{supplier_name} 크롤러 없음"
                    continue
                supplier_groups.setdefault(supplier_name, []).append((idx, item))

            # 4-2b. cart-sync 공급사 락 **전량 선취득**.
            # 하나라도 못 잡으면 아무 공급사도 전송하지 않은 상태에서 재큐잉해야 한다.
            # 앞 공급사를 보낸 뒤 뒤 공급사에서 실패하면 재실행 때 앞 공급사가 중복 주문된다.
            _lock_failed = None
            for _sn in supplier_groups:
                if not getattr(self._crawlers.get(_sn), "SUPPORTS_CART_SYNC", False):
                    continue
                _tok = _acquire_cart_lock(self._redis, monitor_id, _sn)
                if not _tok:
                    _lock_failed = _sn
                    break
                cart_locks[_sn] = _tok
            if _lock_failed:
                for _sn, _tok in cart_locks.items():
                    _release_cart_lock(self._redis, monitor_id, _sn, _tok)
                conn.rollback()
                if _requeue_delayed(self._redis, job, "%s 락 미획득" % _lock_failed):
                    cur.execute('UPDATE domae_order_batches SET status = %s WHERE id = %s',
                                ("pending", batch_id))
                    conn.commit()
                    logger.warning("batch_order 락 미획득 → 전량 미전송 재큐잉: %s", _lock_failed)
                    return
                cur.execute("""
                    UPDATE domae_order_batches SET status = %s, "completedAt" = now()
                    WHERE id = %s
                """, ("failed", batch_id))
                cur.execute(
                    'UPDATE domae_cloud_orders SET success = false, "confirmedQuantity" = NULL, message = %s '
                    'WHERE "batchId" = %s AND success IS NULL ' + _KEEP_UNCONFIRMED,
                    ("장바구니 락 획득 실패 (재시도 소진)", batch_id))
                conn.commit()
                return

            # 4-2. 사전 실패 항목 기록
            for idx, msg in pre_fail.items():
                item = items[idx]
                fail_count += 1
                fail_lines.append(
                    f" · [{item.get('supplier', '?')}] {item.get('product_name', '')} ×{item.get('quantity', 1)} — {msg}"
                )
                utc_now = datetime.now(timezone.utc).replace(tzinfo=None)
                # reasonCode='pre_fail' — 외부 전송과 무관한 사전검증 실패임을 표시한다.
                # 멱등 게이트가 이것을 "이미 주문됨" 으로 오인하면 정상 품목까지 취소된다.
                _record_order_result(
                    cur, monitor_id, batch_id, item.get("supplier") or "", item,
                    success=False, message=msg, reason_code="pre_fail")
                cart_item_id = item.get("cart_item_id")
                if cart_item_id:
                    cur.execute(
                        'UPDATE domae_cart_items SET "failedAt" = %s, "failReason" = %s WHERE id = %s',
                        (utc_now, msg, cart_item_id)
                    )
            conn.commit()

            # 4-3. 도매상별 일괄 주문.
            # cart-sync 공급사를 먼저 처리한다. 뒤로 밀면 앞 공급사 처리 시간만큼
            # lease 가 소진돼 전송 직전 락 상실로 떨어질 확률이 커진다.
            _ordered = sorted(
                supplier_groups.items(),
                key=lambda kv: 0 if kv[0] in cart_locks else 1)
            _sent_any = False   # 외부 전송을 한 번이라도 했는가 (재큐잉 안전성 판단)
            try:
              for supplier_name, group_items in _ordered:
                cred = credentials[supplier_name]
                crawler_cls = self._crawlers[supplier_name]
                # 보호 커밋(_mark_sending)이 성공한 뒤에만 True — 그 전 예외는 전송 전이다.
                # _sent_any 는 커밋 전에 True 가 되므로 전송 여부 판정에 쓰면 안 된다.
                _send_committed = False

                if supplier_name not in logged_in_crawlers:
                    crawler = crawler_cls()
                    crawler.cart_snapshot = CartSnapshot(self._redis, monitor_id, supplier_name, account=cred.get("login_id", ""))
                    crawler.login(cred.get("login_id", ""), cred.get("login_pw", ""))
                    logged_in_crawlers[supplier_name] = crawler
                crawler = logged_in_crawlers[supplier_name]

                # 토큰 캐시를 요구하는 크롤러(TJ팜 등)를 위해 각 product_name 으로
                # 선행 search 를 수행해 내부 캐시를 채운다.
                for _, _gi in group_items:
                    _name = _gi.get("product_name") or ""
                    if _name:
                        try:
                            crawler.search(_name)
                        except Exception as _e:
                            logger.warning("batch_order 선행 search 실패 [%s / %s]: %s", supplier_name, _name, _e)

                batch_items = [
                    {"product_id": item.get("product_id"), "quantity": item.get("quantity", 1), "product_name": item.get("product_name", ""), "insurance_code": item.get("insurance_code"), "unit": item.get("unit")}
                    for _, item in group_items
                ]

                try:
                    # SUPPORTS_CART_SYNC 모드: 장바구니 = 재고 선점 상태.
                    # 대조 → 페이로드 검증 → 커밋 → 전송을 같은 락 안에서 수행한다.
                    if getattr(crawler_cls, "SUPPORTS_CART_SYNC", False):
                        lock_token = cart_locks.get(supplier_name)   # 루프 전 선취득분
                        if not lock_token:
                            raise _LockUnavailable(supplier_name)
                        if True:
                            rec = reconcile_cart(
                                conn, self._redis, monitor_id, supplier_name, crawler,
                                exclude_batch_id=batch_id,
                                renew_cb=lambda: _renew_cart_lock(
                                    self._redis, monitor_id, supplier_name, lock_token))
                            if rec.fatal:
                                conn.rollback()
                                raise _ReconcileFatal(rec.fatal)

                            # 페이로드에 없는데 웹 카트에 있는 품목은 전송에 함께 실린다.
                            # 기록 없이 주문되지 않도록 배치에 편입하고 pending 행까지 만든다.
                            _payload = [it for _, it in group_items]
                            _added = _absorb_reconciled(
                                cur, monitor_id, batch_id, supplier_name,
                                rec, _payload, batch_items)
                            for _i in range(len(group_items), len(_payload)):
                                group_items.append((_i, _payload[_i]))

                            # 대조 결과·pending 행은 외부 전송 전에 durable 해야 한다 (설계 3.3)
                            conn.commit()

                            if not _renew_cart_lock(self._redis, monitor_id,
                                                    supplier_name, lock_token):
                                # 데이터 오류가 아니라 동시성 실패다. 전송 안 했으므로
                                # 실패로 확정하지 말고 재시도 가능 상태로 올린다.
                                raise _LockUnavailable(supplier_name)

                            _sent_any = True
                        _mark_sending(conn, cur, batch_id, supplier_name,
                                      _cart_ids(it for _, it in group_items))
                        _send_committed = True
                        results = crawler.order_batch(batch_items)
                    else:
                        _sent_any = True
                        _mark_sending(conn, cur, batch_id, supplier_name,
                                      _cart_ids(it for _, it in group_items))
                        _send_committed = True
                        results = crawler.order_batch(batch_items)
                except _LockUnavailable as e:
                    # lease 만료 등으로 전송 직전 락을 잃은 경우. 이 공급사는 전송하지 않았다.
                    conn.rollback()
                    if not _sent_any:
                        # 아직 아무 공급사도 전송하지 않았으면 통째로 재큐잉해도 안전하다.
                        if _requeue_delayed(self._redis, job, "lease 상실: %s" % supplier_name):
                            cur.execute(
                                'UPDATE domae_order_batches SET status = %s WHERE id = %s',
                                ("pending", batch_id))
                            conn.commit()
                            logger.warning("batch_order lease 상실 → 미전송 재큐잉: %s",
                                           supplier_name)
                            return
                    # 이미 전송한 공급사가 있으면 재큐잉이 중복 주문을 만든다 → 이 공급사만 실패.
                    logger.error("batch_order 락 상실 [%s] (전송분 존재로 재큐잉 안 함): %s",
                                 supplier_name, e)
                    results = [OrderResult(success=False, message=str(e), reason_code="not_sent", no_retry=True) for _ in group_items]
                except _ReconcileFatal as e:
                    conn.rollback()
                    logger.error("batch_order 대조 중단 [%s]: %s", supplier_name, e)
                    results = [OrderResult(success=False, message=f"대조 중단: {e}", reason_code="not_sent", no_retry=True) for _ in group_items]
                except Exception as e:
                    # 대조 중 만든 pending 행이 커밋되면 아무도 마감하지 않는 고아가 된다.
                    # 대조분을 통째로 버린다. 보호 커밋 전 예외는 전송 전이므로 not_sent.
                    conn.rollback()
                    _rc = "send_unknown" if _send_committed else "not_sent"
                    results = [OrderResult(success=False, message=str(e), reason_code=_rc,
                                           no_retry=not _send_committed) for _ in group_items]

                # 길이 불일치 방어
                if len(results) != len(group_items):
                    logger.warning("order_batch 반환 길이 불일치: %s expected=%d got=%d",
                                   supplier_name, len(group_items), len(results))
                    from domae_mcp.core.crawlers.base import OrderResult as _OR
                    while len(results) < len(group_items):
                        results.append(_OR(success=False, message="결과 누락", reason_code="send_unknown"))

                # ── 1차 결과 분류 (성공/실패 분리) ──
                succeeded = []  # [(idx, item, result)]
                failed = []     # [(idx, item, result)]
                for (idx, item), result in zip(group_items, results):
                    if result.success:
                        succeeded.append((idx, item, result))
                    else:
                        failed.append((idx, item, result))

                # ── 실패 항목 1회 재시도 (안전 검증은 각 크롤러 내장) ──
                # 재시도 불필요한 실패 사유 (재시도해도 결과 동일)
                # NOTE: "재고 부족"/"재고 0" 은 크롤러 내부(PartialStockFallbackMixin)가 수량 조정
                #       재시도를 이미 수행했으므로 워커 레벨 재시도는 무의미.
                #       "수량 조정 후 재시도 실패" 는 Phase 2 submit 도 실패한 것이므로
                #       원래 수량으로 다시 재시도하면 카트 오염 위험만 있음.
                retryable = []
                # 복산 등 SUPPORTS_CART_SYNC 도매상은 전송 단위가 품목이 아니라 장바구니 전체다.
                # 품목별 order() 재시도는 (a) 요청 외 품목 가드에 걸려 항상 거부되고
                # (b) 통과하더라도 카트 전체를 다시 전송해 중복 주문이 된다.
                _skip_item_retry = getattr(crawler_cls, "SUPPORTS_CART_SYNC", False)
                for entry in ([] if _skip_item_retry else failed):
                    if _is_item_retryable(entry[2]):
                        retryable.append(entry)

                if retryable:
                    logger.info("batch_order 재시도: %s 실패 %d건 중 %d건 재시도",
                                supplier_name, len(failed), len(retryable))
                    time.sleep(2)
                    still_failed = []
                    for idx, item, orig_result in retryable:
                        pid = item.get("product_id")
                        qty = item.get("quantity", 1)
                        if not pid:
                            still_failed.append((idx, item, orig_result))
                            continue
                        try:
                            _retry_name = item.get("product_name") or ""
                            if _retry_name:
                                try:
                                    crawler.search(_retry_name)
                                except Exception:
                                    pass
                            retry_result = crawler.order(pid, qty, product_name=_retry_name,
                                                         insurance_code=item.get("insurance_code"))
                            if retry_result.success:
                                logger.info("batch_order 재시도 성공: %s pid=%s", supplier_name, pid)
                                retry_result.retried = True
                                succeeded.append((idx, item, retry_result))
                            else:
                                still_failed.append((idx, item, retry_result))
                        except Exception as e:
                            logger.warning("batch_order 재시도 실패: %s pid=%s err=%s", supplier_name, pid, e)
                            still_failed.append((idx, item, OrderResult(success=False, message=str(e), reason_code="send_unknown")))
                    # failed를 재시도 불가 + 재시도 실패로 재구성
                    failed = [e for e in failed if not _is_item_retryable(e[2])] + still_failed

                # ── 결과 DB 기록 ──
                for idx, item, result in succeeded:
                    order_id_val = getattr(result, "order_id", None)
                    order_message = getattr(result, "message", "")
                    order_price = item.get("price")
                    original_qty = int(item.get("quantity", 1))
                    adjusted_qty = getattr(result, "adjusted_quantity", None)
                    avail_stock = getattr(result, "available_stock", None)
                    rcode = getattr(result, "reason_code", None) or "ok"
                    utc_now = datetime.now(timezone.utc).replace(tzinfo=None)
                    _record_order_result(
                        cur, monitor_id, batch_id, supplier_name, item,
                        success=True, message=order_message, order_id=order_id_val,
                        adjusted_qty=adjusted_qty, avail_stock=avail_stock, reason_code=rcode, confirmed_qty=confirmed_quantity(result, item.get("quantity", 1)))
                    success_count += 1
                    if rcode == "stock_adjusted" and adjusted_qty is not None:
                        missing_qty_total += max(0, original_qty - int(adjusted_qty))
                        adjusted_count += 1
                        _tg_line = (f" · [{supplier_name}] {item.get('product_name', '')}"
                                    f" — 요청 {original_qty} → 주문 {adjusted_qty} (재고 {avail_stock})"
                                    f"{_retry_tag(result)}")
                        adjusted_lines.append(_tg_line)
                    else:
                        success_lines.append(_batch_success_line(supplier_name, item, result))
                    cart_item_id = item.get("cart_item_id")
                    if cart_item_id:
                        # 부분 성공(stock_adjusted)은 부족분을 실패 상태로 남긴다 (auto_order 와 같은 규칙)
                        _act, _keep, _why = cart_action_after_order(item, result)
                        if _act == "keep_failed":
                            cur.execute(
                                'UPDATE domae_cart_items SET quantity = %s, "failedAt" = %s, "failReason" = %s WHERE id = %s',
                                (_keep, utc_now, _why, cart_item_id))
                        else:
                            cur.execute('DELETE FROM domae_cart_items WHERE id = %s', (cart_item_id,))

                for idx, item, result in failed:
                    order_message = getattr(result, "message", "")
                    order_price = item.get("price")
                    original_qty = int(item.get("quantity", 1))
                    adjusted_qty = getattr(result, "adjusted_quantity", None)
                    avail_stock = getattr(result, "available_stock", None)
                    rcode = getattr(result, "reason_code", None) or "other"
                    utc_now = datetime.now(timezone.utc).replace(tzinfo=None)
                    _record_order_result(
                        cur, monitor_id, batch_id, supplier_name, item,
                        success=_db_success(result), message=order_message,
                        adjusted_qty=adjusted_qty, avail_stock=avail_stock, reason_code=rcode, confirmed_qty=confirmed_quantity(result, item.get("quantity", 1)))
                    if rcode != "send_unknown":
                        fail_count += 1   # 미확정은 실패로 세지 않는다
                    if rcode == "send_unknown":
                        unconfirmed_lines.append(
                            f" · [{supplier_name}] {item.get('product_name', '')} ×{original_qty}")
                    elif rcode == "stock_zero":
                        missing_qty_total += original_qty
                        _tg_line = (f" · [{supplier_name}] {item.get('product_name', '')}"
                                    f" — 요청 {original_qty} (재고 0)")
                        missing_lines.append(_tg_line)
                    else:
                        _tg_line = f" · [{supplier_name}] {item.get('product_name', '')} ×{original_qty}"
                        fail_lines.append(_tg_line + (f" — {order_message}" if order_message else ""))
                    cart_item_id = item.get("cart_item_id")
                    if cart_item_id:
                        cur.execute(
                            'UPDATE domae_cart_items SET "failedAt" = %s, "failReason" = %s WHERE id = %s',
                            (utc_now, "전송 결과 확인 필요 — 도매몰 주문내역 확인" if rcode == "send_unknown" else order_message, cart_item_id)
                        )

                # 각 supplier 처리 후 부분 커밋 (row 기록만 확정)
                conn.commit()

                time.sleep(1)  # 도매상 간 딜레이

            finally:
                # 공급사 루프 종료 시 즉시 해제. 바깥 finally 에도 같은 해제가 있지만
                # 토큰 비교 방식이라 두 번 호출해도 무해하고, 여기서 먼저 풀면
                # 후속 처리(텔레그램 발송 등) 동안 락을 잡고 있지 않는다.
                # 여기서 예외가 나가면 바깥 finally 의 putconn 까지 건너뛸 수 있으므로 삼킨다.
                try:
                    for _sn, _tok in cart_locks.items():
                        _release_cart_lock(self._redis, monitor_id, _sn, _tok)
                except Exception as _e:
                    logger.error("장바구니 락 해제 실패(루프) [%s]: %s", monitor_id, _e)

            # 5. batch 마감 — DB 행 기준 재집계. 미확정이 남으면 processing 유지.
            _close_batch(cur, batch_id, "completed", adjusted_count, missing_qty_total)
            conn.commit()

            # 6. 텔레그램 알림
            if telegram_chat_id:
                try:
                    from domae_mcp.cloud.notifier import Notifier
                    parts = ["📦 도매 일괄주문 완료\n"]
                    # 헤드라인: 누락 수량 요약
                    if missing_qty_total > 0:
                        parts.append(f"📉 {missing_qty_total}개 주문 누락되었습니다\n")
                    if success_lines:
                        parts.append(f"✅ 성공 {len(success_lines)}건")
                        parts.extend(success_lines[:10])
                        if len(success_lines) > 10:
                            parts.append(f" ... 외 {len(success_lines) - 10}건")
                    if adjusted_lines:
                        if success_lines:
                            parts.append("")
                        parts.append(f"⚠️ 수량 조정 {len(adjusted_lines)}건")
                        parts.extend(adjusted_lines[:10])
                        if len(adjusted_lines) > 10:
                            parts.append(f" ... 외 {len(adjusted_lines) - 10}건")
                    if missing_lines:
                        if success_lines or adjusted_lines:
                            parts.append("")
                        parts.append(f"❌ 재고 0 주문 누락 {len(missing_lines)}건")
                        parts.extend(missing_lines[:10])
                        if len(missing_lines) > 10:
                            parts.append(f" ... 외 {len(missing_lines) - 10}건")
                    if fail_lines:
                        if success_lines or adjusted_lines or missing_lines:
                            parts.append("")
                        parts.append(f"❌ 실패 {len(fail_lines)}건")
                        parts.extend(fail_lines[:10])
                        if len(fail_lines) > 10:
                            parts.append(f" ... 외 {len(fail_lines) - 10}건")
                    if unconfirmed_lines:
                        parts.append("")
                        parts.append(f"⚠ 전송 결과 확인 필요 {len(unconfirmed_lines)}건 "
                                     "(실패 아님 — 도매몰 주문내역을 확인해 주세요)")
                        parts.extend(unconfirmed_lines[:10])
                    msg = "\n".join(parts)
                    Notifier.send_telegram(telegram_chat_id, msg)
                except Exception as e:
                    logger.warning("텔레그램 알림 실패: %s", e)

            logger.info("batch_order 완료: batch=%s success=%d fail=%d", batch_id, success_count, fail_count)

        except Exception as e:
            conn.rollback()
            logger.error("batch_order 실패 [%s]: %s", batch_id, e, exc_info=True)
            try:
                cur = conn.cursor()
                # 서버가 만든 pending 주문행을 마감하지 않으면 success=null 로 영구 잔존한다.
                # crawler.login() 이 공급사 루프의 inner try 밖이라 로그인 실패로도 여기 온다.
                _fail_pending_rows(cur, batch_id, str(e)[:200])
                # DB 행 기준으로 마감. 미확정이 남았으면 failed 가 아니라 processing 유지.
                _close_batch(cur, batch_id, "failed", adjusted_count, missing_qty_total)
                conn.commit()
            except Exception:
                pass
            # 부분 성공이라도 텔레그램 알림 발송
            if telegram_chat_id and (success_lines or adjusted_lines or missing_lines or fail_lines
                                     or unconfirmed_lines):
                try:
                    from domae_mcp.cloud.notifier import Notifier
                    parts = [f"📦 도매 일괄주문 오류 (일부 처리됨)\n"]
                    if missing_qty_total > 0:
                        parts.append(f"📉 {missing_qty_total}개 주문 누락되었습니다\n")
                    if success_lines:
                        parts.append(f"✅ 성공 {len(success_lines)}건")
                        parts.extend(success_lines[:10])
                    if adjusted_lines:
                        if success_lines:
                            parts.append("")
                        parts.append(f"⚠️ 수량 조정 {len(adjusted_lines)}건")
                        parts.extend(adjusted_lines[:10])
                    if missing_lines:
                        if success_lines or adjusted_lines:
                            parts.append("")
                        parts.append(f"❌ 재고 0 주문 누락 {len(missing_lines)}건")
                        parts.extend(missing_lines[:10])
                    if fail_lines:
                        if success_lines or adjusted_lines or missing_lines:
                            parts.append("")
                        parts.append(f"❌ 실패 {len(fail_lines)}건")
                        parts.extend(fail_lines[:10])
                    if unconfirmed_lines:
                        parts.append("")
                        parts.append(f"⚠ 전송 결과 확인 필요 {len(unconfirmed_lines)}건 (실패 아님)")
                        parts.extend(unconfirmed_lines[:10])
                    parts.append(f"\n⚠️ 오류: {str(e)[:100]}")
                    Notifier.send_telegram(telegram_chat_id, "\n".join(parts))
                except Exception:
                    pass
        finally:
            # 획득 이후 어느 경로로 빠져나가든 락을 돌려준다. 놓치면 TTL(120초) 동안
            # 해당 도매상의 주문·동기화가 전부 막힌다. (토큰 비교라 이중 해제는 무해)
            # 락 해제가 실패해도 putconn 은 반드시 실행한다 — 커넥션 누수가 락 잔존보다
            # 훨씬 치명적이다(락은 TTL 로 풀리지만 커넥션은 프로세스 재시작까지 안 돌아온다).
            try:
                for _sn, _tok in cart_locks.items():
                    _release_cart_lock(self._redis, monitor_id, _sn, _tok)
            except Exception as _e:
                logger.error("장바구니 락 해제 실패 [%s]: %s", monitor_id, _e)
            self._db_pool.putconn(conn)

    def auto_order(self, job: dict):
        """자동주문 — 단일 도매상 장바구니 주문 실행 + 텔레그램 알림 + SSE 알림"""
        monitor_id = job["monitor_id"]
        batch_id = job["batch_id"]
        supplier_name = job["supplier"]
        scheduled_at = job.get("scheduled_at", "")
        items = job.get("items", [])

        conn = self._get_conn()
        telegram_chat_id = None
        success_items = []
        failed_items = []
        unconfirmed_items = []
        fallback_outcomes = []

        try:
            cur = conn.cursor()

            # 1. 배치 소유권 **원자적** 획득 (batch_order 와 동일 계약).
            # SELECT 후 UPDATE 로 나누면 동시 실행 둘 다 통과해 중복 주문이 된다.
            cur.execute("""
                UPDATE domae_order_batches SET status = 'processing'
                WHERE id = %s AND "monitorId" = %s AND status = 'pending'
                RETURNING id
            """, (batch_id, monitor_id))
            if cur.fetchone() is None:
                conn.rollback()
                logger.warning("auto_order 중단: batch=%s 소유권 획득 실패", batch_id)
                return
            conn.commit()

            if _finalize_if_confirmed(conn, cur, batch_id, "이미 확정된 배치 — 재실행 중단"):
                return

            utc_now = datetime.now(timezone.utc).replace(tzinfo=None)

            # 2. credentials + telegramChatId 조회 (isActive 체크 포함)
            cur.execute("""
                SELECT m.credentials, m."telegramChatId", m."supplierOrder", m."autoFallbackOrder"
                FROM domae_cloud_monitors m
                WHERE m.id = %s AND m."isActive" = true
            """, (monitor_id,))
            row = cur.fetchone()
            if not row:
                self._update_auto_order_log(conn, monitor_id, batch_id, "failed", "모니터 없음 또는 비활성")
                _fail_pending_rows(cur, batch_id, "모니터 없음 또는 비활성")
                cur.execute(
                    'UPDATE domae_order_batches SET status = %s WHERE id = %s',
                    ("failed", batch_id)
                )
                conn.commit()
                return

            raw_creds = row[0]
            credentials = self._decrypt_creds(raw_creds)
            telegram_chat_id = row[1]
            supplier_order = row[2] if isinstance(row[2], list) else []
            auto_fallback = bool(row[3])

            # 3. 크롤러 로드
            if not self._crawlers_loaded:
                self._load_crawlers(conn)

            # 4. 사전 검증
            cred = credentials.get(supplier_name)
            if not cred:
                self._update_auto_order_log(conn, monitor_id, batch_id, "failed", f"{supplier_name} 계정 미등록")
                _fail_pending_rows(cur, batch_id, f"{supplier_name} 계정 미등록")
                cur.execute('UPDATE domae_order_batches SET status = %s WHERE id = %s', ("failed", batch_id))
                conn.commit()
                if telegram_chat_id:
                    self._send_auto_order_telegram(telegram_chat_id, supplier_name, [], items,
                                                   global_error=f"{supplier_name} 계정 미등록",
                                                   scheduled_at=scheduled_at)
                return

            crawler_cls = self._crawlers.get(supplier_name)
            if not crawler_cls:
                self._update_auto_order_log(conn, monitor_id, batch_id, "failed", f"{supplier_name} 크롤러 없음")
                _fail_pending_rows(cur, batch_id, f"{supplier_name} 크롤러 없음")
                cur.execute('UPDATE domae_order_batches SET status = %s WHERE id = %s', ("failed", batch_id))
                conn.commit()
                if telegram_chat_id:
                    self._send_auto_order_telegram(telegram_chat_id, supplier_name, [], items,
                                                   global_error=f"{supplier_name} 크롤러 없음",
                                                   scheduled_at=scheduled_at)
                return

            # 5. 로그인 + 주문 실행
            crawler = crawler_cls()
            crawler.cart_snapshot = CartSnapshot(self._redis, monitor_id, supplier_name, account=cred.get("login_id", ""))
            if not crawler.login(cred.get("login_id", ""), cred.get("login_pw", "")):
                raise CrawlerError("로그인 실패")

            # 토큰 캐시 준비용 선행 search (TJ팜 등)
            for _it in items:
                _name = _it.get("product_name") or ""
                if _name:
                    try:
                        crawler.search(_name)
                    except Exception as _e:
                        logger.warning("auto_order 선행 search 실패 [%s / %s]: %s", supplier_name, _name, _e)

            batch_items = [
                {"product_id": item.get("product_id"), "quantity": item.get("quantity", 1), "product_name": item.get("product_name", ""), "insurance_code": item.get("insurance_code"), "unit": item.get("unit")}
                for item in items
            ]

            # SUPPORTS_CART_SYNC 도매상은 동시 cart_sync 작업과 경합 방지를 위해 락 획득
            ao_token = None
            is_cart_sync = getattr(crawler_cls, "SUPPORTS_CART_SYNC", False)
            send_committed = False   # 보호 커밋 성공 후에만 True (그 전 예외는 전송 전)
            try:
                if is_cart_sync:
                    ao_token = _acquire_cart_lock(self._redis, monitor_id, supplier_name)
                    if not ao_token:
                        raise _LockUnavailable(supplier_name)

                    rec = reconcile_cart(
                        conn, self._redis, monitor_id, supplier_name, crawler,
                        exclude_batch_id=batch_id,
                        renew_cb=lambda: _renew_cart_lock(
                            self._redis, monitor_id, supplier_name, ao_token))
                    if rec.fatal:
                        conn.rollback()
                        raise _ReconcileFatal(rec.fatal)

                    # 페이로드에 없는 웹 카트 품목도 전송에 실리므로 배치에 편입하고
                    # pending 주문행까지 전송 전에 만든다.
                    _absorb_reconciled(cur, monitor_id, batch_id, supplier_name,
                                       rec, items, batch_items)

                    conn.commit()   # 외부 전송 전 durable (설계 3.3)

                    if not _renew_cart_lock(self._redis, monitor_id, supplier_name, ao_token):
                        raise _LockUnavailable(supplier_name)

                _mark_sending(conn, cur, batch_id, supplier_name, _cart_ids(items))
                send_committed = True
                results = crawler.order_batch(batch_items)
            except _LockUnavailable as e:
                conn.rollback()
                if _requeue_delayed(self._redis, job, str(e)):
                    cur.execute('UPDATE domae_order_batches SET status = %s WHERE id = %s',
                                ("pending", batch_id))
                    conn.commit()
                    logger.warning("auto_order 락 미획득 → 재큐잉: %s", supplier_name)
                    _release_cart_lock(self._redis, monitor_id, supplier_name, ao_token)
                    return
                results = [OrderResult(success=False, message=str(e), reason_code="not_sent") for _ in items]
            except _ReconcileFatal as e:
                conn.rollback()
                logger.error("auto_order 대조 중단 [%s]: %s", supplier_name, e)
                results = [OrderResult(success=False, message=f"대조 중단: {e}", reason_code="not_sent") for _ in items]
            except Exception as e:
                # 대조 중 만든 pending 행이 커밋되면 고아가 된다 (batch_order 와 동일 처리)
                conn.rollback()
                _rc = "send_unknown" if send_committed else "not_sent"
                results = [OrderResult(success=False, message=str(e), reason_code=_rc) for _ in items]
            finally:
                _release_cart_lock(self._redis, monitor_id, supplier_name, ao_token)

            # 길이 불일치 방어
            if len(results) != len(items):
                logger.warning("auto_order order_batch 반환 길이 불일치: %s expected=%d got=%d",
                               supplier_name, len(items), len(results))
                from domae_mcp.core.crawlers.base import OrderResult as _OR
                while len(results) < len(items):
                    results.append(_OR(success=False, message="결과 누락", reason_code="send_unknown"))

            success_count = 0
            fail_count = 0
            fallback_needs = []

            for item, result in zip(items, results):
                rcode = getattr(result, "reason_code", None)
                order_message = getattr(result, "message", "")
                unconfirmed = (not result.success) and rcode == "send_unknown"
                utc_now = datetime.now(timezone.utc).replace(tzinfo=None)

                # 접수 여부 불명은 확정 실패가 아니다 — success 를 NULL 로 남긴다
                _record_order_result(
                    cur, monitor_id, batch_id, supplier_name, item,
                    success=_db_success(result),
                    message=order_message, order_id=getattr(result, "order_id", None),
                    adjusted_qty=getattr(result, "adjusted_quantity", None),
                    avail_stock=getattr(result, "available_stock", None),
                    reason_code=rcode, confirmed_qty=confirmed_quantity(result, item.get("quantity", 1)))

                if result.success:
                    success_count += 1
                    req = int(item.get("quantity", 1))
                    got = getattr(result, "adjusted_quantity", None) or req
                    success_items.append({**item, "quantity": got, "requested_quantity": req,
                                          "retried": bool(getattr(result, "retried", False))})
                elif unconfirmed:
                    unconfirmed_items.append(item)
                else:
                    fail_count += 1
                    failed_items.append({**item, "message": order_message, "_src": item})

                cart_item_id = item.get("cart_item_id")
                action, keep_qty, why = cart_action_after_order(item, result)
                if cart_item_id:
                    if action == "delete":
                        cur.execute('DELETE FROM domae_cart_items WHERE id = %s', (cart_item_id,))
                    elif action == "keep_failed":
                        # 일부만 주문됐다. 남은 수량을 지우면 약국이 모르는 사이 사라진다.
                        cur.execute(
                            'UPDATE domae_cart_items SET quantity = %s, "failedAt" = %s, "failReason" = %s WHERE id = %s',
                            (keep_qty, utc_now, why, cart_item_id))
                    else:  # fail, hold — 실패 상태로 묶어 다음 자동주문에서 빠지게 한다
                        cur.execute(
                            'UPDATE domae_cart_items SET "failedAt" = %s, "failReason" = %s WHERE id = %s',
                            (utc_now, why, cart_item_id))

                need = fallback_need_qty(item, result) if auto_fallback else 0
                if need > 0:
                    fallback_needs.append((item, need))

            # 6. 원 주문 결과·장바구니 반영을 먼저 확정한다 (배치 마감은 대체주문 뒤)
            conn.commit()

            fallback_outcomes = []
            if fallback_needs:
                fconn = None
                try:
                    fconn = self._get_conn()
                    rec = FallbackRecorder(
                        fconn, monitor_id, batch_id, supplier_name, _record_order_result,
                        id_fn=_generate_cuid,
                        now_fn=lambda: datetime.now(timezone.utc).replace(tzinfo=None))
                    available = {s for s in credentials if self._crawlers.get(s)}
                    candidates = next_suppliers(supplier_order, supplier_name, available)
                    opened = {}

                    def _open(sup):
                        if sup not in opened:
                            c = self._crawlers[sup]()
                            c.cart_snapshot = CartSnapshot(self._redis, monitor_id, sup, account=credentials[sup].get("login_id", ""))
                            # 로그인 실패는 전송 전이다 — 예외로 다음 순번에 넘기고 캐시하지 않는다
                            if not c.login(credentials[sup].get("login_id", ""), credentials[sup].get("login_pw", "")):
                                raise CrawlerError(f"{sup} 로그인 실패")
                            opened[sup] = c
                        return opened[sup]

                    def _is_sync(sup):
                        return getattr(self._crawlers[sup], "SUPPORTS_CART_SYNC", False)

                    def _lock(sup):
                        return _acquire_cart_lock(self._redis, monitor_id, sup) if _is_sync(sup) else "nolock"

                    def _renew(sup, token):
                        return True if token == "nolock" else _renew_cart_lock(self._redis, monitor_id, sup, token)

                    def _unlock(sup, token):
                        if token and token != "nolock":
                            _release_cart_lock(self._redis, monitor_id, sup, token)

                    fallback_outcomes = run_fallback(
                        fallback_needs, candidates, _open, _lock, _renew, _unlock,
                        rec.pending, rec.result, rec.unconfirmed,
                        check_unconfirmed=rec.check_unconfirmed)
                    for o in fallback_outcomes:
                        action, keep_qty, why = cart_action_after_fallback(o)
                        try:
                            rec.apply_cart(o.item.get("cart_item_id"), action, keep_qty, why)
                        except Exception as e:
                            logger.error("대체주문 장바구니 반영 실패 cart=%s: %s", o.item.get("cart_item_id"), e)
                        logger.info("대체주문 %s: %s → %s (%s)", o.state, supplier_name, o.supplier or "-", o.message)
                except Exception as e:
                    logger.error("대체주문 처리 실패 — 원 주문 결과에는 영향 없음: %s", e, exc_info=True)
                finally:
                    if fconn is not None:
                        self._db_pool.putconn(fconn)

            # 6-1. 배치 마감 — 원 주문·대체주문 행 기준 재집계. 미확정이 남으면 processing.
            _close_batch(cur, batch_id, "completed")
            conn.commit()

            # 7. DomaeAutoOrderLog 상태 업데이트. 미확정은 실패가 아니라 '확인 필요'다.
            has_shortfall = any(i["requested_quantity"] > i["quantity"] for i in success_items)
            needs_check = bool(unconfirmed_items) or any(
                o.state in NEEDS_CHECK_STATES for o in fallback_outcomes)
            if fail_count == 0 and not has_shortfall:
                log_status = "unconfirmed" if needs_check else "success"
            elif success_count > 0:
                log_status = "partial_fail"
            else:
                log_status = "failed"
            self._update_auto_order_log(conn, monitor_id, batch_id, log_status)

            # 8. 텔레그램 알림 (실패 품목은 대체 도매 검색 + 인라인 버튼)
            if telegram_chat_id:
                self._send_auto_order_telegram(
                    telegram_chat_id, supplier_name, success_items, failed_items,
                    conn=conn, monitor_id=monitor_id, credentials=credentials,
                    scheduled_at=scheduled_at, unconfirmed_items=unconfirmed_items, fallback_outcomes=fallback_outcomes,
                )

            # 9. SSE 결과 알림 (Redis publish)
            try:
                self._redis.publish(f"domae:notifications:{monitor_id}", json.dumps({
                    "type": "auto_order_result",
                    "supplier": supplier_name,
                    "status": log_status,
                    "count": len(success_items),
                    "totalPrice": sum((i.get("price") or 0) * int(i.get("quantity", 0)) for i in success_items),
                    "shortfall": sum(int(i.get("requested_quantity", 0)) - int(i.get("quantity", 0))
                                     for i in success_items),
                    "unconfirmed": len(unconfirmed_items),
                    "fallbackOrdered": sum(1 for o in fallback_outcomes if o.state == "ordered"),
                }))
            except Exception as e:
                logger.warning("auto_order SSE publish 실패: %s", e)

            logger.info("auto_order 완료: batch=%s supplier=%s success=%d fail=%d",
                        batch_id, supplier_name, success_count, fail_count)

        except Exception as e:
            conn.rollback()
            logger.error("auto_order 실패 [%s/%s]: %s", batch_id, supplier_name, e, exc_info=True)
            try:
                cur = conn.cursor()
                _fail_pending_rows(cur, batch_id, str(e)[:200])
                # 미확정이 남았으면 failed 가 아니라 processing 유지 (DB 행 기준)
                _close_batch(cur, batch_id, "failed")
                conn.commit()
            except Exception:
                pass
            self._update_auto_order_log(conn, monitor_id, batch_id, "failed", str(e)[:200])
            # 부분 성공이라도 텔레그램 알림
            if telegram_chat_id and (success_items or failed_items or unconfirmed_items):
                self._send_auto_order_telegram(
                    telegram_chat_id, supplier_name, success_items, failed_items,
                    conn=conn, monitor_id=monitor_id, credentials=credentials,
                    scheduled_at=scheduled_at, unconfirmed_items=unconfirmed_items, fallback_outcomes=fallback_outcomes,
                )
        finally:
            self._db_pool.putconn(conn)

    def _update_auto_order_log(self, conn, monitor_id: str, batch_id: str, status: str, message: str = None):
        """DomaeAutoOrderLog 상태 업데이트 (batchId 기준)"""
        try:
            cur = conn.cursor()
            if message:
                cur.execute("""
                    UPDATE domae_auto_order_logs SET status = %s, message = %s
                    WHERE "monitorId" = %s AND "batchId" = %s
                """, (status, message, monitor_id, batch_id))
            else:
                cur.execute("""
                    UPDATE domae_auto_order_logs SET status = %s
                    WHERE "monitorId" = %s AND "batchId" = %s
                """, (status, monitor_id, batch_id))
            conn.commit()
        except Exception as e:
            logger.warning("auto_order_log 상태 업데이트 실패: %s", e)

    def _send_auto_order_telegram(self, chat_id: str, supplier: str, success_items: list,
                                  failed_items: list, global_error: str = None,
                                  conn=None, monitor_id: str = None, credentials: dict = None,
                                  scheduled_at: str = "", unconfirmed_items=None, fallback_outcomes=None):
        """실제 주문 수량과 미확정 결과를 알린다. 미확정 품목에는 재주문 버튼을 제공하지 않는다."""
        try:
            from domae_mcp.cloud.notifier import Notifier
            unconfirmed_items = unconfirmed_items or []
            fallback_outcomes = fallback_outcomes or []
            now_str = scheduled_at or datetime.now(timezone(timedelta(hours=9))).strftime("%H:%M")
            if global_error:
                Notifier.send_telegram(chat_id, f"❌ 자동주문 실패 ({supplier}, {now_str})\n\n{global_error}\n\n수동으로 확인해주세요.")
                return
            handled = {id(o.item) for o in fallback_outcomes if o.state in ("ordered", *NEEDS_CHECK_STATES)}
            button_items = [it for it in failed_items if id(it.get("_src", it)) not in handled]
            keyboard = []
            if button_items and credentials and monitor_id:
                available = [s for s in credentials if s != supplier and self._crawlers.get(s)]
                for item in button_items[:5]:
                    row = []
                    for alt in self._search_alternatives(item.get("product_name", ""), available, credentials)[:3]:
                        mid = Notifier._sanitize_cb_field(monitor_id, 8)
                        sup = Notifier._sanitize_cb_field(alt["supplier"], 10)
                        pid = Notifier._sanitize_cb_field(alt["product_id"], 16)
                        cb = f"AO:{mid}:{sup}:{pid}:{item.get('quantity', 1)}"
                        if len(cb.encode("utf-8")) <= 64:
                            price = f" {alt['price']:,}원" if alt.get("price") else ""
                            row.append({"text": f"{alt['supplier']}{price}", "callback_data": cb})
                    if row:
                        keyboard.append(row)
            # 미확정은 실패가 아니다 — 확정 실패·부족분이 없으면 '확인 필요'로만 알린다
            partial = bool(failed_items or any(i.get("requested_quantity", i.get("quantity", 0)) > i.get("quantity", 0) for i in success_items))
            needs_check = bool(unconfirmed_items) or any(o.state in NEEDS_CHECK_STATES for o in fallback_outcomes)
            if partial:
                title = "⚠️ 자동주문 부분 완료" if success_items else "❌ 자동주문 실패"
            elif needs_check:
                title = "⚠️ 자동주문 결과 확인 필요"
            else:
                title = "✅ 자동주문 완료"
            lines = [f"{title} ({supplier}, {now_str})\n"]
            if success_items:
                lines.append("✅ 성공:")
                lines.extend(format_ordered_line(it) for it in success_items[:10])
                if len(success_items) > 10:
                    lines.append(f"... 외 {len(success_items) - 10}건")
                total = sum((it.get("price") or 0) * int(it.get("quantity", 0)) for it in success_items)
                lines.append(f"총 {len(success_items)}건, {total:,}원 주문 완료")
            if button_items:
                lines.append("\n❌ 실패:")
                lines.extend(f"• {it.get('product_name', '')} {it.get('quantity', 1)}개 — {it.get('message', '주문 실패')}" for it in button_items[:10])
            if unconfirmed_items:
                lines.append("\n⚠ 전송 결과 확인 필요 (도매몰 주문내역을 확인해 주세요):")
                lines.extend(f"• {it.get('product_name', '')} {it.get('quantity', 1)}개" for it in unconfirmed_items)
            if fallback_outcomes:
                lines.append("\n대체 주문:")
                for o in fallback_outcomes:
                    mark = {"ordered": "↪", "unconfirmed": "⚠", "blocked": "⚠"}.get(o.state, "✗")
                    text = f"{o.supplier} {o.ordered_qty}개 주문 완료" if o.state == "ordered" else o.message
                    lines.append(f"{mark} {o.item.get('product_name', '')} — {text}")
            if keyboard:
                lines.append("\n대체 도매에서 주문하려면 아래 버튼을 누르세요:")
            Notifier.send_telegram(chat_id, "\n".join(lines), reply_markup={"inline_keyboard": keyboard} if keyboard else None)
        except Exception as e:
            logger.warning("자동주문 텔레그램 알림 실패: %s", e)

    def _search_alternatives(self, product_name: str, available_suppliers: list, credentials: dict) -> list:
        """다른 도매에서 해당 품목 검색 — 재고 있는 결과만 반환 (병렬)."""
        results = []

        def _search_one(sup: str):
            try:
                cred = credentials.get(sup)
                if not cred:
                    return None
                crawler_cls = self._crawlers.get(sup)
                if not crawler_cls:
                    return None
                crawler = crawler_cls()
                crawler.login(cred.get("login_id", ""), cred.get("login_pw", ""))
                search_results = crawler.search(product_name)
                for r in search_results:
                    if r.quantity and r.quantity > 0:
                        return {
                            "supplier": sup,
                            "product_id": r.product_id,
                            "product_name": r.product_name,
                            "price": r.price,
                            "quantity": r.quantity,
                        }
                return None
            except Exception as e:
                logger.debug("대체 검색 실패 [%s/%s]: %s", sup, product_name, e)
                return None

        # with 미사용 이유는 execute() 와 동일 — 멈춘 스레드를 기다리면 워커 전체가 멈춘다.
        executor = ThreadPoolExecutor(max_workers=min(len(available_suppliers), 5))
        try:
            futures = {executor.submit(_search_one, sup): sup for sup in available_suppliers}
            try:
                for future in as_completed(futures, timeout=SUPPLIER_SCAN_TIMEOUT):
                    try:
                        result = future.result()
                        if result:
                            results.append(result)
                    except Exception:
                        pass
            except FuturesTimeoutError:
                logger.error(
                    "대체 도매 검색 시한 초과 — 미완료 %d건",
                    sum(1 for f in futures if not f.done()),
                )
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

        return results

    def _alt_buttons(self, monitor_id, product_name, product_id, quantity, tried, credentials):
        from domae_mcp.cloud.notifier import Notifier
        remaining = [s for s in credentials if s not in tried and self._crawlers.get(s)]
        if not remaining:
            return []
        alternatives = self._search_alternatives(product_name or product_id, remaining, credentials)
        buttons = []
        for alt in alternatives[:3]:
            mid = Notifier._sanitize_cb_field(monitor_id, 8)
            sup = Notifier._sanitize_cb_field(alt["supplier"], 10)
            pid = Notifier._sanitize_cb_field(alt["product_id"], 16)
            data = f"AO:{mid}:{sup}:{pid}:{quantity}"
            if len(data.encode("utf-8")) <= 64:
                price = f" {alt['price']:,}원" if alt.get("price") else ""
                buttons.append({"text": f"{alt['supplier']}{price}", "callback_data": data})
        return [buttons] if buttons else []

    @staticmethod
    def _notify_callback(chat_id, message_id, original_text, text, buttons=None):
        from domae_mcp.cloud.notifier import Notifier
        # Explicit empty markup removes the original order buttons on edit.
        markup = {"inline_keyboard": buttons or []}
        try:
            if message_id:
                ok = Notifier.edit_message(chat_id, message_id, html.escape(original_text + "\n\n" + text), reply_markup=markup)
                if not ok:
                    Notifier.send_telegram(chat_id, html.escape(text), reply_markup=markup)
            else:
                Notifier.send_telegram(chat_id, html.escape(text), reply_markup=markup)
        except Exception:
            logger.exception("텔레그램 주문 결과 알림 실패")

    def _notify_unknown(self, chat_id, message_id, original_text, supplier, product_name, result, quantity):
        text = f"⚠ [{supplier}] {product_name} 전송 결과 확인 필요 — 도매몰 주문내역을 확인하세요"
        confirmed = confirmed_quantity(result, quantity)
        if confirmed is not None:
            text += f" (확정 수량 {confirmed}개)"
        self._notify_callback(chat_id, message_id, original_text, text)

    def _notify_failed(self, chat_id, message_id, original_text, supplier, result, buttons=None):
        text = f"❌ {supplier} 주문 실패: {result.message or '주문 실패'}"
        if buttons:
            text += "\n\n다른 도매에서 주문하려면 아래 버튼을 누르세요:"
        self._notify_callback(chat_id, message_id, original_text, text, buttons)

    def _telegram_callback_order(self, conn, cur, crawler, prefix, monitor_id, supplier,
                                 product_id, quantity, chat_id, message_id, original_text,
                                 product_name, price, insurance_code, unit, tried, credentials):
        """Commit a permanent send marker before crossing the crawler boundary."""
        attempt_key = f"{prefix}:{monitor_id}:{chat_id}:{message_id}:{supplier}:{product_id}"
        row_id = _generate_cuid()
        cur.execute('''INSERT INTO domae_cloud_orders
            (id,"monitorId",supplier,"productName",quantity,price,success,"productId",
             message,"reasonCode","attemptKey","orderedAt",unit,"insuranceCode")
            VALUES (%s,%s,%s,%s,%s,%s,NULL,%s,'전송 결과 확인 중','send_unknown',%s,%s,%s,%s)
            ON CONFLICT ("attemptKey") DO NOTHING RETURNING id''',
            (row_id,monitor_id,supplier,product_name,quantity,price,product_id,
             attempt_key,datetime.now(timezone.utc).replace(tzinfo=None),unit,insurance_code))
        inserted = cur.fetchone()
        conn.commit()
        if not inserted:
            self._notify_callback(chat_id, message_id, original_text,
                                  "이미 처리된 주문입니다. 결과는 주문 내역에서 확인하세요.")
            return
        try:
            result = crawler.order(product_id, quantity, product_name=product_name or "",
                                   insurance_code=insurance_code)
        except Exception:
            logger.exception("텔레그램 주문 전송 결과 확인 불가")
            result = OrderResult(success=False, reason_code="send_unknown", message="주문 중 오류 — 확인 필요")
        if not isinstance(result, OrderResult):
            result = OrderResult(success=False, reason_code="send_unknown", message="주문 결과 형식 이상 — 확인 필요")
        if not result.success and result.reason_code in (None, "other"):
            result.reason_code = "send_unknown"
        if result.reason_code == "send_unknown":
            result.success = False
        if result.success and confirmed_quantity(result, quantity) is None:
            result.success = False
            result.reason_code = "send_unknown"
        db_ok = _db_success(result)
        confirmed = confirmed_quantity(result, quantity)
        batch_id = None
        try:
            if prefix == "tg":
                batch_id = _generate_cuid()
                now = datetime.now(timezone.utc).replace(tzinfo=None)
                cur.execute('''INSERT INTO domae_order_batches
                    (id,"monitorId",status,"totalItems","successCount","failCount","createdAt","completedAt")
                    VALUES (%s,%s,%s,1,%s,%s,%s,%s)''',
                    (batch_id,monitor_id,"processing" if db_ok is None else "completed",
                     1 if db_ok else 0,1 if db_ok is False else 0,now,None if db_ok is None else now))
            cur.execute('''UPDATE domae_cloud_orders SET success=%s,"reasonCode"=%s,
                "confirmedQuantity"=%s,message=%s,"orderId"=%s,"adjustedQuantity"=%s,
                "availableStock"=%s,"batchId"=%s WHERE id=%s''',
                (db_ok,result.reason_code,confirmed,result.message or "",result.order_id,
                 result.adjusted_quantity,result.available_stock,batch_id,row_id))
            conn.commit()
        except Exception:
            logger.exception("텔레그램 주문 결과 기록 실패 — 전송 확인 필요 행 보존")
            try:
                conn.rollback()
            except Exception:
                logger.exception("텔레그램 주문 연결 rollback 실패")
            self._notify_unknown(chat_id,message_id,original_text,supplier,product_name,
                                 OrderResult(success=False,reason_code="send_unknown"),quantity)
            return
        if db_ok is None:
            self._notify_unknown(chat_id,message_id,original_text,supplier,product_name,result,quantity)
        elif db_ok:
            total = f"\n주문금액 {price * confirmed:,}원" if price else ""
            self._notify_callback(chat_id,message_id,original_text,
                                  f"✅ [{supplier}] {product_name} {confirmed}개 주문 완료{_retry_tag(result)}{total}")
        else:
            buttons = None
            if result.reason_code in ("not_sent", "stock_zero"):
                try:
                    buttons = self._alt_buttons(monitor_id,product_name,product_id,quantity,tried,credentials)
                except Exception:
                    logger.exception("텔레그램 대체 도매 검색 실패")
            self._notify_failed(chat_id,message_id,original_text,supplier,result,buttons)

    def auto_order_retry(self, job: dict):
        """텔레그램 대체 도매 주문 (단일 품목) — AO 콜백 버튼 핸들러.

        성공 시 메시지 편집, 실패 시 남은 도매로 인라인 버튼 재표시.
        """
        monitor_id = job.get("monitor_id")
        monitor_prefix = job.get("monitor_prefix", "")
        supplier_name = job["supplier"]
        product_id = job["product_id"]
        quantity = job["quantity"]
        chat_id = job["chat_id"]
        message_id = job.get("message_id")
        original_text = job.get("original_text", "")
        tried_suppliers = job.get("tried_suppliers", [])

        conn = self._get_conn()
        try:
            from domae_mcp.cloud.notifier import Notifier

            # 입력 검증: monitor_id가 있으면 직접 사용, 없으면 prefix 필요
            if not monitor_id and (not monitor_prefix or len(monitor_prefix) != 8):
                Notifier.send_order_result(
                    chat_id, message_id, original_text,
                    product_id, supplier_name, quantity, 0,
                    success=False, error_msg="잘못된 요청",
                )
                return

            # 1. 모니터 조회 + chat_id 소유권 검증
            cur = conn.cursor()
            if monitor_id:
                # monitor_id가 있으면 직접 조회
                cur.execute("""
                    SELECT m.id, m.credentials
                    FROM domae_cloud_monitors m
                    WHERE m.id = %s
                      AND m."isActive" = true
                      AND m."telegramChatId" = %s
                    LIMIT 1
                """, (monitor_id, chat_id))
            else:
                # monitor_prefix로 LIKE 검색
                cur.execute("""
                    SELECT m.id, m.credentials
                    FROM domae_cloud_monitors m
                    WHERE LEFT(m.id, 8) = %s
                      AND m."isActive" = true
                      AND m."telegramChatId" = %s
                    LIMIT 1
                """, (monitor_prefix, chat_id))
            row = cur.fetchone()
            if not row:
                Notifier.send_order_result(
                    chat_id, message_id, original_text,
                    product_id, supplier_name, quantity, 0,
                    success=False, error_msg="권한 없음",
                )
                return

            monitor_id = row[0]
            credentials = self._decrypt_creds(row[1])

            # 2. 크롤러 로드
            if not self._crawlers_loaded:
                self._load_crawlers(conn)

            cred = credentials.get(supplier_name)
            if not cred:
                Notifier.send_order_result(
                    chat_id, message_id, original_text,
                    product_id, supplier_name, quantity, 0,
                    success=False, error_msg=f"{supplier_name} 계정 미등록",
                )
                return

            crawler_cls = self._crawlers.get(supplier_name)
            if not crawler_cls:
                Notifier.send_order_result(
                    chat_id, message_id, original_text,
                    product_id, supplier_name, quantity, 0,
                    success=False, error_msg=f"{supplier_name} 크롤러 없음",
                )
                return

            # 3. 주문 실행
            crawler = crawler_cls()
            crawler.cart_snapshot = CartSnapshot(self._redis, monitor_id, supplier_name, account=cred.get("login_id", ""))
            crawler.login(cred.get("login_id", ""), cred.get("login_pw", ""))

            # 제품명/가격 조회
            product_name = product_id
            price = 0
            insurance_code = None
            try:
                search_results = crawler.search(product_id)
                for sr in search_results:
                    if sr.product_id == product_id:
                        product_name = sr.product_name
                        price = sr.price or 0
                        insurance_code = sr.insurance_code
                        break
            except Exception:
                pass

            # TJ팜 등 토큰 캐시 기반 크롤러용 선행 search
            if product_name and product_name != product_id:
                try:
                    crawler.search(product_name)
                except Exception:
                    pass

            _reject = _reject_unreconciled(supplier_name, crawler_cls, "이 주문")
            if _reject:
                raise RuntimeError(_reject)
            self._telegram_callback_order(
                conn, cur, crawler, "ao", monitor_id, supplier_name, product_id,
                quantity, chat_id, message_id, original_text, product_name, price,
                insurance_code, None, tried_suppliers + [supplier_name], credentials,
            )

        except Exception as e:
            logger.error("auto_order_retry 실패: %s", e, exc_info=True)
            try:
                from domae_mcp.cloud.notifier import Notifier
                Notifier.send_order_result(
                    chat_id, message_id, original_text,
                    product_id, supplier_name, quantity, 0,
                    success=False, error_msg="서버 오류",
                )
            except Exception:
                pass
        finally:
            try:
                conn.rollback()
            except Exception:
                self._db_pool.putconn(conn, close=True)
            else:
                self._db_pool.putconn(conn)

    def urgent_order_immediate(self, job: dict):
        """긴급주문 즉시 1회 실행 — response_key로 결과 반환"""
        monitor_id = job["monitor_id"]
        response_key = job["response_key"]
        urgent_order_id = job["urgent_order_id"]
        suppliers_info = job.get("suppliers", [])
        remaining_qty = job.get("remaining_quantity", 0)

        conn = self._get_conn()
        try:
            cur = conn.cursor()

            # credentials 조회
            cur.execute("""
                SELECT m.credentials
                FROM domae_cloud_monitors m
                WHERE m.id = %s AND m."isActive" = true
            """, (monitor_id,))
            row = cur.fetchone()
            if not row:
                self._redis.lpush(response_key, json.dumps({"success": False, "message": "모니터 없음"}))
                return

            raw_creds = row[0]
            credentials = self._decrypt_creds(raw_creds)

            if not self._crawlers_loaded:
                self._load_crawlers(conn)

            # 재고 확인용 검색 키워드. job payload 에 없으면 DB 에서 조회한다.
            # (product_id 로 검색하면 백제처럼 복합키를 쓰는 도매는 항상 0건이 된다)
            product_name = job.get("product_name") or ""
            if not product_name:
                cur.execute(
                    'SELECT "productName" FROM domae_urgent_orders WHERE id = %s',
                    (urgent_order_id,),
                )
                name_row = cur.fetchone()
                product_name = (name_row[0] if name_row else "") or ""
            if not product_name:
                logger.error(
                    "urgent_order_immediate: product_name 확보 실패 urgent=%s — 재고 확인 불가",
                    urgent_order_id,
                )

            filled = 0
            details = []

            # 도매별 결과 수집 (합산 로그용)
            supplier_results = {}  # {supplier_name: {"qty": int}}
            any_success = False
            first_scanned_at = None

            for sup_info in suppliers_info:
                if filled >= remaining_qty:
                    break

                supplier_name = sup_info["supplier"]
                product_id_val = sup_info["product_id"]
                need = remaining_qty - filled

                cred = credentials.get(supplier_name)
                if not cred:
                    details.append({"supplier": supplier_name, "quantity": 0, "success": False, "message": "계정 미등록"})
                    supplier_results[supplier_name] = {"qty": 0}
                    continue

                crawler_cls = self._crawlers.get(supplier_name)
                if not crawler_cls:
                    details.append({"supplier": supplier_name, "quantity": 0, "success": False, "message": "크롤러 없음"})
                    supplier_results[supplier_name] = {"qty": 0}
                    continue

                try:
                    crawler = crawler_cls()
                    crawler.cart_snapshot = CartSnapshot(self._redis, monitor_id, supplier_name, account=cred.get("login_id", ""))
                    crawler.login(cred.get("login_id", ""), cred.get("login_pw", ""))

                    # 재고 확인
                    scanned_at = datetime.now(timezone.utc).replace(tzinfo=None)
                    if first_scanned_at is None:
                        first_scanned_at = scanned_at
                    # product_name 으로 검색 후 product_id 로 매칭 (위 주석 참조)
                    search_results = crawler.search(product_name)
                    available = 0
                    insurance_code = None
                    for sr in search_results:
                        if sr.product_id == product_id_val and sr.quantity and sr.quantity > 0:
                            available = sr.quantity
                            insurance_code = sr.insurance_code
                            break

                    if available == 0:
                        logger.info(
                            "urgent immediate: 재고 없음 또는 매칭 실패 [%s] name=%r pid=%s (검색 %d건)",
                            supplier_name, product_name, product_id_val, len(search_results),
                        )
                        details.append({"supplier": supplier_name, "quantity": 0, "success": False, "message": "재고 없음"})
                        supplier_results[supplier_name] = {"qty": 0}
                        continue

                    # 주문 실행
                    order_qty = min(need, available)
                    _reject = _reject_unreconciled(supplier_name, crawler_cls, "이 주문")
                    if _reject:
                        raise RuntimeError(_reject)
                    result = crawler.order(product_id_val, order_qty, product_name=product_name,
                                           insurance_code=insurance_code)

                    if result.success:
                        filled += order_qty
                        details.append({"supplier": supplier_name, "quantity": order_qty, "success": True,
                                        "message": getattr(result, "message", "주문 완료")})
                        supplier_results[supplier_name] = {"qty": order_qty}
                        any_success = True
                    else:
                        details.append({"supplier": supplier_name, "quantity": 0, "success": False,
                                        "message": getattr(result, "message", "주문 실패")})
                        supplier_results[supplier_name] = {"qty": 0}

                except Exception as e:
                    details.append({"supplier": supplier_name, "quantity": 0, "success": False, "message": str(e)})
                    supplier_results.setdefault(supplier_name, {"qty": 0})
                    logger.warning("urgent immediate [%s/%s]: %s", supplier_name, urgent_order_id, e)

                time.sleep(0.5)

            # 합산 로그 1건 INSERT
            if supplier_results:
                utc_now = datetime.now(timezone.utc).replace(tzinfo=None)
                message_parts = [f"{s} {r['qty']}" for s, r in supplier_results.items()]
                total_ordered = sum(r["qty"] for r in supplier_results.values())
                cur.execute("""
                    INSERT INTO domae_urgent_logs
                    (id, "urgentOrderId", supplier, "orderedQuantity", success, message, "scannedAt", "orderedAt")
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """, (
                    _generate_cuid(), urgent_order_id, "",
                    total_ordered, any_success,
                    ", ".join(message_parts),
                    first_scanned_at or utc_now, utc_now,
                ))

                # 오래된 로그 자동 삭제 (20건 초과 시)
                cur.execute("""
                    DELETE FROM domae_urgent_logs
                    WHERE "urgentOrderId" = %s
                    AND id NOT IN (
                        SELECT id FROM domae_urgent_logs
                        WHERE "urgentOrderId" = %s
                        ORDER BY "orderedAt" DESC LIMIT 20
                    )
                """, (urgent_order_id, urgent_order_id))

                conn.commit()

            # filledQuantity 업데이트
            if filled > 0:
                cur.execute("""
                    UPDATE domae_urgent_orders
                    SET "filledQuantity" = "filledQuantity" + %s
                    WHERE id = %s
                """, (filled, urgent_order_id))

                # 목표 달성 체크
                cur.execute(
                    'SELECT "filledQuantity", "totalQuantity" FROM domae_urgent_orders WHERE id = %s',
                    (urgent_order_id,)
                )
                uo_row = cur.fetchone()
                total_filled = uo_row[0] if uo_row else filled
                total_qty = uo_row[1] if uo_row else remaining_qty
                completed = total_filled >= total_qty

                if completed:
                    utc_now = datetime.now(timezone.utc).replace(tzinfo=None)
                    cur.execute(
                        'UPDATE domae_urgent_orders SET active = false, "completedAt" = %s WHERE id = %s',
                        (utc_now, urgent_order_id)
                    )
                conn.commit()
            else:
                total_filled = 0
                total_qty = remaining_qty
                completed = False

            self._redis.lpush(response_key, json.dumps({
                "success": filled > 0,
                "filled_quantity": filled,
                "total_filled": total_filled,
                "total_quantity": total_qty,
                "completed": completed,
                "details": details,
            }))

            logger.info("urgent_order_immediate 완료: urgent=%s filled=%d", urgent_order_id, filled)

        except Exception as e:
            logger.error("urgent_order_immediate 실패: %s", e, exc_info=True)
            self._redis.lpush(response_key, json.dumps({"success": False, "message": str(e)}))
        finally:
            self._db_pool.putconn(conn)

    def verify_credentials(self, job: dict):
        """도매 계정 로그인 검증"""
        response_key = job["response_key"]
        supplier_name = job["supplier"]
        login_id = job["login_id"]
        login_pw = job["login_pw"]

        conn = self._get_conn()
        try:
            if not self._crawlers_loaded:
                self._load_crawlers(conn)

            crawler_cls = self._crawlers.get(supplier_name)
            if not crawler_cls:
                self._redis.lpush(response_key, json.dumps({
                    "verified": False, "message": f"{supplier_name} 크롤러를 찾을 수 없습니다."
                }))
                return

            try:
                crawler = crawler_cls()
                result = crawler.login(login_id, login_pw)
                if result:
                    self._redis.lpush(response_key, json.dumps({
                        "verified": True, "message": "로그인 성공"
                    }))
                else:
                    self._redis.lpush(response_key, json.dumps({
                        "verified": False, "message": "아이디 또는 비밀번호가 올바르지 않습니다."
                    }))
            except Exception as e:
                result = None
                self._redis.lpush(response_key, json.dumps({
                    "verified": False, "message": str(e)
                }))

            logger.info("verify_credentials: %s → %s", supplier_name, "성공" if result else "실패")

        except Exception as e:
            logger.error("verify_credentials 실패: %s", e)
            self._redis.lpush(response_key, json.dumps({
                "verified": False, "message": str(e)
            }))
        finally:
            self._db_pool.putconn(conn)

    def _notify_deletion_stuck(self, conn, monitor_id, supplier_name, product_id):
        """삭제의도 확정이 재시도까지 실패했을 때 사용자에게 알린다.

        조용히 두면 미확인 tombstone 이 남아 나중에 정상 추가한 품목까지 삭제된다.
        """
        try:
            cur = conn.cursor()
            cur.execute(
                'INSERT INTO domae_notifications '
                '(id, "monitorId", type, category, title, body, "isRead", "createdAt") '
                "VALUES (%s, %s, 'cart_deletion_stuck', 'domae', %s, %s, false, now())",
                (_generate_cuid(), monitor_id, "%s 장바구니 삭제 확인 실패" % supplier_name,
                 "제품 %s 의 삭제 확인을 기록하지 못했습니다. 해당 제품을 다시 담으면 "
                 "정상화됩니다." % product_id))
            conn.commit()
        except Exception as e:
            conn.rollback()
            logger.warning("삭제 정체 알림 실패: %s", e)


    def cart_sync(self, job: dict):
        """장바구니 동기화 — PharmSquare 장바구니 변경을 도매몰에 실시간 반영.
        SUPPORTS_CART_SYNC=True인 도매상(복산 등)에서만 동작.
        액션: cart_sync_add, cart_sync_update, cart_sync_remove"""
        action = job["action"]
        monitor_id = job["monitor_id"]
        supplier_name = job["supplier"]
        product_id = job["product_id"]
        quantity = job.get("quantity", 0)
        price = job.get("price", 0)
        cart_item_id = job.get("cart_item_id")
        response_key = job.get("response_key")

        conn = self._get_conn()
        try:
            cur = conn.cursor()
            cur.execute("""
                SELECT m.credentials
                FROM domae_cloud_monitors m
                WHERE m.id = %s AND m."isActive" = true
            """, (monitor_id,))
            row = cur.fetchone()
            if not row:
                self._cart_sync_respond(response_key, False, "모니터 없음")
                return

            credentials = self._decrypt_creds(row[0])
            cred = credentials.get(supplier_name)
            if not cred:
                self._cart_sync_respond(response_key, False, f"{supplier_name} 계정 미등록")
                return

            if not self._crawlers_loaded:
                self._load_crawlers(conn)

            crawler_cls = self._crawlers.get(supplier_name)
            if not crawler_cls:
                self._cart_sync_respond(response_key, False, f"{supplier_name} 크롤러 없음")
                return

            if not getattr(crawler_cls, "SUPPORTS_CART_SYNC", False):
                self._cart_sync_respond(response_key, False, f"{supplier_name} 장바구니 동기화 미지원")
                return

            # 필수 메서드 존재 확인 (다른 도매상 추가 시 AttributeError 방지)
            required_methods = ["_add_to_cart", "_get_cart_items", "remove_from_cart", "update_cart_qty"]
            missing = [m for m in required_methods if not hasattr(crawler_cls, m)]
            if missing:
                msg = f"{supplier_name} 크롤러에 필수 메서드 없음: {missing}"
                self._cart_sync_respond(response_key, False, msg)
                self._cart_sync_update_status(conn, cart_item_id, "failed", msg)
                return

            crawler = crawler_cls()
            login_ok = crawler.login(cred.get("login_id", ""), cred.get("login_pw", ""))
            if not login_ok:
                self._cart_sync_respond(response_key, False, f"{supplier_name} 로그인 실패")
                self._cart_sync_update_status(conn, cart_item_id, "failed", f"{supplier_name} 로그인 실패")
                return

            # 도매상별 락 (batch_order와 경합 방지)
            # 주문 경로와 **같은 헬퍼**를 써야 한다. 예전처럼 고정값 "1" + 30초 TTL +
            # 무조건 DELETE 를 쓰면, 만료 후 주문이 잡은 락을 이 코드가 지워버린다.
            lock_token = _acquire_cart_lock(self._redis, monitor_id, supplier_name, retries=0)
            if not lock_token:
                # 주문 진행 중이면 동기화 스킵 (주문이 우선)
                self._cart_sync_respond(response_key, False, "주문 진행 중 — 동기화 대기")
                self._cart_sync_update_status(conn, cart_item_id, "pending")
                return

            try:
                success = False
                message = ""

                if action == "cart_sync_add":
                    crawler._add_to_cart(product_id, quantity, price=price)
                    cart = crawler._get_cart_items()
                    # 존재 여부만 보면 "5개 요청했는데 2개만 담김" 을 synced 로 기록한다.
                    _got = sum(int(str(c.get("qty", 0)).replace(",", "") or 0)
                               for c in cart if c["pc"] == product_id)
                    found = _got == int(quantity or 0)
                    if found:
                        success = True
                        message = "장바구니 동기화 완료"
                    else:
                        success = False
                        message = "장바구니 담기 실패 (도매몰 거부)"

                elif action == "cart_sync_update":
                    crawler.update_cart_qty(product_id, quantity, price=price)
                    cart = crawler._get_cart_items()
                    _got = sum(int(str(c.get("qty", 0)).replace(",", "") or 0)
                               for c in cart if c["pc"] == product_id)
                    success = _got == int(quantity or 0)
                    message = ("수량 변경 동기화 완료" if success
                               else f"수량 변경 실패 (요청 {quantity} / 실제 {_got})")

                elif action == "cart_sync_remove":
                    crawler.remove_from_cart(product_id)
                    cart = crawler._get_cart_items()
                    still_exists = any(c["pc"] == product_id for c in cart)
                    # 웹에서 사라진 것을 확인했으면 삭제 의도를 확정한다. 확정하지 않으면
                    # 나중에 같은 제품을 정상 추가해도 대조가 삭제 대상으로 취급한다.
                    _deletion_recorded = True
                    try:
                        _cur = conn.cursor()
                        _cur.execute("SELECT to_regclass('public.domae_cart_deletions')")
                        if _cur.fetchone()[0] is not None:
                            if not still_exists:
                                _cur.execute(
                                    'UPDATE domae_cart_deletions SET "confirmedAt" = now() '
                                    'WHERE "monitorId" = %s AND supplier = %s '
                                    'AND "productId" = %s AND "confirmedAt" IS NULL',
                                    (monitor_id, supplier_name, product_id))
                                try:
                                    self._redis.delete(
                                        f"domae:cart:tombstone:{monitor_id}:{supplier_name}:{product_id}")
                                except Exception:
                                    pass
                            else:
                                _cur.execute(
                                    'UPDATE domae_cart_deletions SET attempts = attempts + 1 '
                                    'WHERE "monitorId" = %s AND supplier = %s '
                                    'AND "productId" = %s AND "confirmedAt" IS NULL '
                                    'RETURNING attempts',
                                    (monitor_id, supplier_name, product_id))
                                _row = _cur.fetchone()
                                _attempts = _row[0] if _row else 0
                                # 미확정 tombstone 을 방치하면 이후 모든 주문에서 대조가
                                # 삭제를 재시도하다 fatal 로 떨어져 해당 도매상 주문이
                                # 무기한 중단된다. 재시도하고, 누적되면 사람에게 알린다.
                                if _attempts >= 5:
                                    self._notify_deletion_stuck(conn, monitor_id,
                                                                supplier_name, product_id)
                                else:
                                    _requeue_delayed(self._redis, job,
                                                     "웹 삭제 실패 재시도 pc=%s (%d회)"
                                                     % (product_id, _attempts))
                            conn.commit()
                    except Exception as _e:
                        conn.rollback()
                        _deletion_recorded = False
                        logger.warning("삭제 의도 갱신 실패 (pc=%s): %s", product_id, _e)
                    if still_exists:
                        success = False
                        message = "삭제 실패 (항목 잔존)"
                    elif not _deletion_recorded:
                        # 웹에서는 지워졌지만 삭제 의도를 확정하지 못했다. 성공으로 응답하면
                        # 미확인 tombstone 이 조용히 남아 나중에 정상 추가분까지 지운다.
                        # 로그만 남기면 아무도 재시도하지 않으므로 같은 잡을 지연 재큐잉한다.
                        # remove_from_cart 는 이미 없는 품목에 대해 즉시 반환하므로 재실행이 안전하다.
                        success = False
                        if _requeue_delayed(self._redis, job,
                                            "삭제의도 확정 실패 pc=%s" % product_id):
                            message = "웹 삭제 완료 · 삭제의도 확정 실패 — 재시도 예약"
                        else:
                            message = "웹 삭제 완료 · 삭제의도 확정 실패 (재시도 소진)"
                            self._notify_deletion_stuck(conn, monitor_id, supplier_name,
                                                        product_id)
                    else:
                        success = True
                        message = "삭제 동기화 완료"

                # DB 동기화 상태 업데이트
                status = "synced" if success else "failed"
                self._cart_sync_update_status(conn, cart_item_id, status, message if not success else None)

                self._cart_sync_respond(response_key, success, message)
                logger.info("cart_sync %s: monitor=%s supplier=%s pid=%s → %s",
                            action, monitor_id, supplier_name, product_id, message)

                # 설계 B — 액션이 끝난 뒤(그 전이 아니라) 나머지 품목을 대조한다.
                # 액션 전에 하면 사용자가 방금 바꾼 수량을 웹 기준으로 되돌린다.
                # 처리 중인 제품은 in-flight 로 제외한다.
                # 여기서의 대조는 드리프트 수렴이 목적이라 실패해도 동기화 결과를 뒤집지 않는다.
                if success:
                    try:
                        rec = reconcile_cart(conn, self._redis, monitor_id, supplier_name,
                                             crawler, in_flight_product_id=product_id)
                        if rec.fatal:
                            conn.rollback()
                            logger.warning("cart_sync 후 대조 중단: %s", rec.fatal)
                        else:
                            conn.commit()
                            if rec.changed:
                                logger.info("cart_sync 후 대조 반영: 고아 %d · 재담기 %d · 보정 %d",
                                            len(rec.added), len(rec.restored), len(rec.adjusted))
                    except Exception as _e:
                        conn.rollback()
                        logger.warning("cart_sync 후 대조 실패: %s", _e)
            finally:
                _release_cart_lock(self._redis, monitor_id, supplier_name, lock_token)

        except Exception as e:
            logger.error("cart_sync 실패 [%s]: %s", action, e, exc_info=True)
            self._cart_sync_respond(response_key, False, str(e))
            self._cart_sync_update_status(conn, cart_item_id, "failed", str(e)[:200])
        finally:
            self._db_pool.putconn(conn)

    def _cart_sync_respond(self, response_key: str | None, success: bool, message: str):
        """cart_sync 결과를 Redis response 채널로 반환 (선택적)"""
        if response_key:
            self._redis.lpush(response_key, json.dumps({
                "success": success, "message": message
            }))

    def _cart_sync_update_status(self, conn, cart_item_id: str | None, status: str, error: str | None = None):
        """DomaeCartItem의 syncStatus 업데이트"""
        if not cart_item_id:
            return
        try:
            cur = conn.cursor()
            utc_now = datetime.now(timezone.utc).replace(tzinfo=None)
            if status == "synced":
                cur.execute(
                    'UPDATE domae_cart_items SET "syncStatus" = %s, "syncError" = NULL, "syncedAt" = %s WHERE id = %s',
                    (status, utc_now, cart_item_id),
                )
            else:
                cur.execute(
                    'UPDATE domae_cart_items SET "syncStatus" = %s, "syncError" = %s WHERE id = %s',
                    (status, error, cart_item_id),
                )
            conn.commit()
        except Exception as e:
            logger.warning("_cart_sync_update_status 실패: %s", e)

    def _persist_cart_release(self, conn, key, receipt):
        """안정된 ID·시각으로 감사 기록 후 커밋한다. 모호한 커밋도 재실행 가능하다."""
        cur = conn.cursor()
        cur.execute('''INSERT INTO domae_order_audit_events
            (id, "monitorId", supplier, "eventType", source, payload, "createdAt")
            VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (id) DO NOTHING''',
            (receipt["id"], receipt["monitorId"], receipt["supplier"], "cart_snapshot_released", "worker",
             json.dumps({"revision": receipt["revision"]}), receipt["releasedAt"]))
        conn.commit()
        CartSnapshot.ack_release(self._redis, key, receipt)

    def recover_cart_releases(self):
        """정상 잡 대기 루프에서 최대 10건을 복구하고 실패는 대기열 뒤로 보낸다.

        승인된 영수증을 사용해 계정 변경 이후에도 복구한다. 전역 SCAN 없이 전용 ZSET만
        읽고, 파싱·DB·rollback 실패를 이 배치 안에 가둬 일반 잡 소비를 막지 않는다.
        """
        try:
            keys = self._redis.zrange(RELEASE_PENDING_KEY, 0, 9)
        except Exception as error:
            logger.warning("장바구니 해제 감사 대기열 조회 실패: %s", type(error).__name__)
            return
        failures = 0
        for key in keys:
            conn = None
            discard_conn = False
            try:
                receipt = CartSnapshot.release_receipt(self._redis, key)
                if receipt is None:
                    # 정상 ACK가 같은 트랜잭션에서 pending 항목도 지운다.
                    continue
                conn = self._get_conn()
                self._persist_cart_release(conn, key, receipt)
            except Exception as error:
                failures += 1
                discard_conn = isinstance(error, (psycopg2.OperationalError, psycopg2.InterfaceError))
                if conn is not None:
                    try:
                        conn.rollback()
                    except Exception:
                        discard_conn = True
                try:
                    CartSnapshot.defer_release(self._redis, key)
                except Exception:
                    # Redis 장애에도 영수증을 ACK하지 않는다. 다음 정상 루프에서 재시도한다.
                    pass
            finally:
                if conn is not None:
                    try:
                        if discard_conn:
                            self._db_pool.putconn(conn, close=True)
                        else:
                            self._db_pool.putconn(conn)
                    except Exception:
                        failures += 1
                        try:
                            conn.close()
                        except Exception:
                            pass
        if failures:
            logger.warning("장바구니 해제 감사 복구 오류 %s건 — 영수증 보존, 다음 루프 재시도", failures)

    def cart_release(self, job: dict):
        """확인 버튼의 소유권을 다시 확인하고 해당 계정·차수만 해제한다."""
        from domae_mcp.cloud.notifier import Notifier
        prefix, supplier = job.get("monitor_prefix", ""), job.get("supplier", "")
        rev, chat_id = job.get("revision"), str(job.get("chat_id", ""))
        binding = job.get("account_binding")
        if not isinstance(binding, str) or not re.fullmatch(r"[A-Za-z0-9_-]{16}", binding):
            return
        if not re.fullmatch(r"[A-Za-z0-9]{8}", prefix) or supplier not in ("티제이팜", "백제", "지오영") or type(rev) is not int or rev <= 0 or not chat_id:
            return
        conn = self._get_conn()
        result = "권한 없음"
        try:
            cur = conn.cursor()
            cur.execute('''SELECT id, credentials FROM domae_cloud_monitors
                WHERE LEFT(id, 8) = %s AND "telegramChatId" = %s AND "isActive" = true
                AND id = %s''', (prefix, chat_id, job.get("monitor_id")))
            row = cur.fetchone()
            if row:
                cred = self._decrypt_creds(row[1]).get(supplier)
                if cred and cred.get("login_id"):
                    store = CartSnapshot(self._redis, row[0], supplier, account=cred["login_id"])
                    if hmac.compare_digest(binding, store.account_binding):
                        result = store.release(rev)
                        if result == "ok":
                            key = store.release_key(rev)
                            receipt = CartSnapshot.release_receipt(self._redis, key)
                            if receipt is not None:
                                self._persist_cart_release(conn, key, receipt)
                    else:
                        result = "도매 계정이 변경되었습니다. 최신 장바구니 확인 알림을 사용해 주세요."
        except Exception:
            conn.rollback()
            logger.exception("장바구니 확인 해제 처리 실패 chat=%s", Notifier._tail(chat_id))
            result = "확인 해제 처리 중 오류가 발생했습니다."
        finally:
            self._db_pool.putconn(conn)
        Notifier.send_telegram(chat_id, "장바구니 확인이 완료되었습니다." if result == "ok" else result)

    def telegram_order(self, job: dict):
        """텔레그램 인라인 버튼으로 접수된 주문 처리.

        monitor_prefix로 모니터를 찾고, supplier/product_id로 주문 실행.
        결과를 원본 텔레그램 메시지에 편집으로 반영.
        """
        monitor_prefix = job["monitor_prefix"]
        supplier_name = job["supplier"]
        product_id = job["product_id"]
        quantity = job["quantity"]
        chat_id = job["chat_id"]
        message_id = job.get("message_id")
        original_text = job.get("original_text", "")

        conn = self._get_conn()
        try:
            from domae_mcp.cloud.notifier import Notifier

            # 입력 검증: monitor_prefix는 정확히 8자 영숫자
            if not monitor_prefix or len(monitor_prefix) != 8:
                Notifier.send_order_result(
                    chat_id, message_id, original_text,
                    product_id, supplier_name, quantity, 0,
                    success=False, error_msg="잘못된 요청",
                )
                return

            # 1. monitor_prefix로 모니터 조회 (LEFT 정확 매칭 + chat_id 소유권 검증)
            cur = conn.cursor()
            cur.execute("""
                SELECT m.id, m.credentials
                FROM domae_cloud_monitors m
                WHERE LEFT(m.id, 8) = %s
                  AND m."isActive" = true
                  AND m."telegramChatId" = %s
                LIMIT 1
            """, (monitor_prefix, chat_id))
            row = cur.fetchone()
            if not row:
                Notifier.send_order_result(
                    chat_id, message_id, original_text,
                    product_id, supplier_name, quantity, 0,
                    success=False, error_msg="권한 없음",
                )
                return

            monitor_id = row[0]
            credentials = self._decrypt_creds(row[1])

            # 2. 크롤러 로드
            if not self._crawlers_loaded:
                self._load_crawlers(conn)

            cred = credentials.get(supplier_name)
            if not cred:
                Notifier.send_order_result(
                    chat_id, message_id, original_text,
                    product_id, supplier_name, quantity, 0,
                    success=False, error_msg=f"{supplier_name} 계정 미등록",
                )
                return

            crawler_cls = self._crawlers.get(supplier_name)
            if not crawler_cls:
                Notifier.send_order_result(
                    chat_id, message_id, original_text,
                    product_id, supplier_name, quantity, 0,
                    success=False, error_msg=f"{supplier_name} 크롤러 없음",
                )
                return

            # 3. 주문 실행
            crawler = crawler_cls()
            crawler.cart_snapshot = CartSnapshot(self._redis, monitor_id, supplier_name, account=cred.get("login_id", ""))
            login_ok = crawler.login(cred.get("login_id", ""), cred.get("login_pw", ""))
            logger.info(
                "telegram_order 로그인: supplier=%s login_ok=%s",
                supplier_name, login_ok,
            )
            if not login_ok:
                Notifier.send_order_result(
                    chat_id, message_id, original_text,
                    product_id, supplier_name, quantity, 0,
                    success=False, error_msg=f"{supplier_name} 로그인 실패",
                )
                return

            # 제품명/가격 조회 — DB 스냅샷에서 우선 조회, 없으면 크롤러 검색
            product_name = product_id
            price = 0
            unit = None
            insurance_code = None
            try:
                cur.execute("""
                    SELECT "productName", price, unit, "insuranceCode"
                    FROM domae_inventory_snapshots
                    WHERE "monitorId" = %s AND "productId" = %s AND supplier = %s
                    ORDER BY "scannedAt" DESC LIMIT 1
                """, (monitor_id, product_id, supplier_name))
                snap = cur.fetchone()
                if snap:
                    product_name = snap[0] or product_id
                    price = snap[1] or 0
                    unit = snap[2]
                    insurance_code = snap[3]
                    logger.info("telegram_order DB 조회: product=%s name=%s", product_id, product_name)
                else:
                    # DB에 없으면 크롤러 검색 fallback
                    search_results = crawler.search(product_id)
                    for sr in search_results:
                        if sr.product_id == product_id:
                            product_name = sr.product_name
                            price = sr.price or 0
                            unit = sr.unit
                            insurance_code = sr.insurance_code
                            break
                    logger.info("telegram_order 크롤러 검색: product_id=%s results=%d", product_id, len(search_results))
            except Exception as e:
                logger.warning("telegram_order 제품 조회 실패: %s", e)

            # TJ팜 등 토큰 캐시 기반 크롤러용 선행 search
            if product_name and product_name != product_id:
                try:
                    crawler.search(product_name)
                except Exception:
                    pass

            _reject = _reject_unreconciled(supplier_name, crawler_cls, "이 주문")
            if _reject:
                raise RuntimeError(_reject)
            self._telegram_callback_order(
                conn, cur, crawler, "tg", monitor_id, supplier_name, product_id,
                quantity, chat_id, message_id, original_text, product_name, price,
                insurance_code, unit, [supplier_name], credentials,
            )

        except Exception as e:
            logger.error("telegram_order 실패: %s", e, exc_info=True)
            try:
                from domae_mcp.cloud.notifier import Notifier
                Notifier.send_order_result(
                    chat_id, message_id, original_text,
                    product_id, supplier_name, quantity, 0,
                    success=False, error_msg="서버 오류",
                )
            except Exception:
                pass
        finally:
            try:
                conn.rollback()
            except Exception:
                self._db_pool.putconn(conn, close=True)
            else:
                self._db_pool.putconn(conn)

    def _process_urgent_orders(self, conn, monitor_id: str, credentials: dict):
        """모니터링 주기 내 활성 긴급주문 처리"""
        cur = conn.cursor()
        cur.execute("""
            SELECT uo.id, uo."productName", uo."totalQuantity", uo."filledQuantity"
            FROM domae_urgent_orders uo
            WHERE uo."monitorId" = %s AND uo.active = true AND uo."filledQuantity" < uo."totalQuantity"
        """, (monitor_id,))
        urgent_orders = cur.fetchall()

        if not urgent_orders:
            return

        for uo_id, product_name, total_qty, filled_qty in urgent_orders:
            remaining = total_qty - filled_qty

            # 이 긴급주문에 등록된 도매상 조회
            cur.execute(
                'SELECT supplier, "productId" FROM domae_urgent_suppliers WHERE "urgentOrderId" = %s',
                (uo_id,)
            )
            suppliers = cur.fetchall()
            filled_this_round = 0

            # 도매별 결과 수집 (합산 로그용)
            supplier_results = {}  # {supplier_name: {"qty": int}}
            any_success = False
            first_scanned_at = None

            for supplier_name, product_id_val in suppliers:
                if filled_this_round >= remaining:
                    supplier_results.setdefault(supplier_name, {"qty": 0})
                    continue

                cred = credentials.get(supplier_name)
                if not cred:
                    supplier_results[supplier_name] = {"qty": 0}
                    continue

                crawler_cls = self._crawlers.get(supplier_name)
                if not crawler_cls:
                    supplier_results[supplier_name] = {"qty": 0}
                    continue

                try:
                    crawler = crawler_cls()
                    crawler.cart_snapshot = CartSnapshot(self._redis, monitor_id, supplier_name, account=cred.get("login_id", ""))
                    crawler.login(cred.get("login_id", ""), cred.get("login_pw", ""))

                    scanned_at = datetime.now(timezone.utc).replace(tzinfo=None)
                    if first_scanned_at is None:
                        first_scanned_at = scanned_at
                    # ⚠️ product_id 가 아니라 product_name 으로 검색한다.
                    # product_id 는 도매별 내부 코드이며 검색 키워드가 아니다.
                    # 예) 백제는 "ITEM_CD|ITEM_GB_CD" 복합키 → keyword 로 넘기면 0건 →
                    #     available=0 으로 빠져 주문이 아예 시도되지 않았다.
                    search_results = crawler.search(product_name)
                    available = 0
                    insurance_code = None
                    for sr in search_results:
                        if sr.product_id == product_id_val and sr.quantity and sr.quantity > 0:
                            available = sr.quantity
                            insurance_code = sr.insurance_code
                            break

                    if available == 0:
                        logger.info(
                            "urgent: 재고 없음 또는 매칭 실패 [%s] name=%r pid=%s (검색 %d건)",
                            supplier_name, product_name, product_id_val, len(search_results),
                        )
                        supplier_results[supplier_name] = {"qty": 0}
                        continue

                    order_qty = min(remaining - filled_this_round, available)
                    _reject = _reject_unreconciled(supplier_name, crawler_cls, "이 주문")
                    if _reject:
                        raise RuntimeError(_reject)
                    result = crawler.order(product_id_val, order_qty, product_name=product_name,
                                           insurance_code=insurance_code)

                    if result.success:
                        filled_this_round += order_qty
                        supplier_results[supplier_name] = {"qty": order_qty}
                        any_success = True

                        # 긴급주문 체결 알림 (건별)
                        try:
                            cur.execute('SELECT "telegramChatId" FROM domae_cloud_monitors WHERE id = %s', (monitor_id,))
                            tg_row = cur.fetchone()
                            if tg_row and tg_row[0]:
                                from domae_mcp.cloud.notifier import Notifier
                                current_filled = (filled_qty or 0) + filled_this_round
                                # 가격 조회
                                price = 0
                                try:
                                    for sr in search_results:
                                        if sr.product_id == product_id_val:
                                            price = sr.price or 0
                                            break
                                except Exception:
                                    pass
                                Notifier.send_urgent_order_result(
                                    chat_id=tg_row[0],
                                    product_name=product_name,
                                    supplier=supplier_name,
                                    quantity=order_qty,
                                    price=price,
                                    filled=current_filled,
                                    total=total_qty,
                                )
                        except Exception as e:
                            logger.warning("긴급주문 알림 실패: %s", e)
                    else:
                        supplier_results[supplier_name] = {"qty": 0}

                    conn.commit()
                    time.sleep(0.5)

                except Exception as e:
                    supplier_results.setdefault(supplier_name, {"qty": 0})
                    logger.warning("urgent process [%s/%s]: %s", uo_id, supplier_name, e)

            # 합산 로그 1건 INSERT
            if supplier_results:
                utc_now = datetime.now(timezone.utc).replace(tzinfo=None)
                message_parts = [f"{s} {r['qty']}" for s, r in supplier_results.items()]
                total_ordered = sum(r["qty"] for r in supplier_results.values())
                cur.execute("""
                    INSERT INTO domae_urgent_logs
                    (id, "urgentOrderId", supplier, "orderedQuantity", success, message, "scannedAt", "orderedAt")
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """, (
                    _generate_cuid(), uo_id, "",
                    total_ordered, any_success,
                    ", ".join(message_parts),
                    first_scanned_at or utc_now, utc_now,
                ))

                # 오래된 로그 자동 삭제 (20건 초과 시)
                cur.execute("""
                    DELETE FROM domae_urgent_logs
                    WHERE "urgentOrderId" = %s
                    AND id NOT IN (
                        SELECT id FROM domae_urgent_logs
                        WHERE "urgentOrderId" = %s
                        ORDER BY "orderedAt" DESC LIMIT 20
                    )
                """, (uo_id, uo_id))

                conn.commit()

            if filled_this_round > 0:
                cur.execute("""
                    UPDATE domae_urgent_orders
                    SET "filledQuantity" = "filledQuantity" + %s
                    WHERE id = %s
                """, (filled_this_round, uo_id))

                cur.execute(
                    'SELECT "filledQuantity", "totalQuantity" FROM domae_urgent_orders WHERE id = %s',
                    (uo_id,)
                )
                row = cur.fetchone()
                if row and row[0] >= row[1]:
                    utc_now = datetime.now(timezone.utc).replace(tzinfo=None)
                    cur.execute(
                        'UPDATE domae_urgent_orders SET active = false, "completedAt" = %s WHERE id = %s',
                        (utc_now, uo_id)
                    )

                conn.commit()

        logger.info("긴급주문 처리 완료: monitor=%s, %d건", monitor_id, len(urgent_orders))
