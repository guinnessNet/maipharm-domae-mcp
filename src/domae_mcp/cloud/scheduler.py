"""클라우드 모니터링 스케줄러"""
import hashlib
import importlib.util
import json
import logging
import os
import secrets
import string
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import psycopg2


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
            with ThreadPoolExecutor(max_workers=min(len(target_suppliers), 8)) as executor:
                futures = {
                    executor.submit(
                        self._search_supplier, name, cls, cred, products
                    ): name
                    for name, (cls, cred) in target_suppliers.items()
                }
                for future in as_completed(futures):
                    supplier = futures[future]
                    try:
                        results = future.result(timeout=120)
                        all_results.extend(results)
                    except Exception as e:
                        logger.error("도매 검색 실패 [%s]: %s", supplier, e)

            # 4. 결과 저장
            if all_results:
                self._save_results(conn, monitor_id, all_results)

            # 5. lastRunAt 업데이트
            utc_now = datetime.now(timezone.utc)
            cur.execute(
                'UPDATE domae_cloud_monitors SET "lastRunAt" = %s, "updatedAt" = %s WHERE id = %s',
                (utc_now, utc_now, monitor_id)
            )
            conn.commit()

            # 6. 변동 감지 + 이벤트별 개별 알림
            all_alerts = []
            for keyword in products:
                keyword_results = [r for r in all_results if r["keyword"] == keyword]
                if keyword_results:
                    alerts = self._detect_alerts(conn, monitor_id, keyword, keyword_results)
                    all_alerts.extend(alerts)

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
            self._db_pool.putconn(conn)

    def _load_crawlers(self, conn):
        """DB에서 크롤러 코드 로드 (동적 import, SHA-256 해시 검증)"""
        cur = conn.cursor()
        cur.execute('SELECT name, code, "codeHash" FROM domae_crawlers WHERE "isActive" = true')
        rows = cur.fetchall()

        cache_dir = tempfile.mkdtemp(prefix="domae_cloud_")

        # base.py import 경로 확보
        # domae_mcp 패키지가 설치되어 있어야 함
        from domae_mcp.core.crawlers.base import BaseCrawler

        for name, code, code_hash in rows:
            # SHA-256 해시 검증
            if code_hash is None:
                logger.error("크롤러 [%s] 로드 거부: codeHash가 NULL입니다. 보안 정책에 의해 해시 없는 코드는 실행할 수 없습니다.", name)
                continue

            computed = hashlib.sha256(code.encode("utf-8")).hexdigest()
            if computed != code_hash:
                logger.error("크롤러 [%s] 로드 거부: 코드 해시 불일치 (expected=%s, computed=%s)", name, code_hash[:16], computed[:16])
                continue

            try:
                file_path = os.path.join(cache_dir, f"{name}.py")
                with open(file_path, "w", encoding="utf-8") as f:
                    f.write(code)

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
        logger.info("크롤러 %d개 로드 완료", len(self._crawlers))

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

    def _search_supplier(self, supplier_name: str, crawler_cls, cred: dict, keywords: list) -> list:
        """도매 1개에 대해 1회 로그인 후 전 품목 순차 검색."""
        results = []
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
                    logger.warning("검색 실패 [%s/%s]: %s", supplier_name, keyword, e)
                time.sleep(0.5)  # 같은 사이트 내 품목 간 딜레이

        except Exception as e:
            logger.error("도매 로그인 실패 [%s]: %s", supplier_name, e)

        return results

    def _save_results(self, conn, monitor_id: str, results: list):
        """검색 결과 DB 저장 (스냅샷 누적 + 24h 정리)"""
        cur = conn.cursor()
        utc_now = datetime.now(timezone.utc)

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
        """24시간 초과 스냅샷을 일별 평균으로 압축 보존."""
        try:
            # 1. 압축 대상 집계 (24h~90일, 같은 날짜에 2건 이상인 그룹)
            cur.execute("""
                SELECT "monitorId", supplier, "productName", unit, "insuranceCode", "productId",
                       DATE("scannedAt") as snap_date,
                       AVG(COALESCE(quantity, 0))::int as avg_qty,
                       AVG(COALESCE(price, 0))::int as avg_price
                FROM domae_inventory_snapshots
                WHERE "monitorId" = %s
                  AND "scannedAt" < NOW() - INTERVAL '24 hours'
                  AND "scannedAt" >= NOW() - INTERVAL '90 days'
                GROUP BY "monitorId", supplier, "productName", unit, "insuranceCode", "productId",
                         DATE("scannedAt")
                HAVING COUNT(*) > 1
            """, (monitor_id,))

            groups = cur.fetchall()

            if groups:
                # 2. 원본 삭제 (24h~90일)
                cur.execute("""
                    DELETE FROM domae_inventory_snapshots
                    WHERE "monitorId" = %s
                      AND "scannedAt" < NOW() - INTERVAL '24 hours'
                      AND "scannedAt" >= NOW() - INTERVAL '90 days'
                """, (monitor_id,))

                # 3. 압축된 일별 대표 레코드 INSERT (scannedAt = 해당 날짜 12:00 UTC)
                for row in groups:
                    mid, supplier, product_name, unit, ins_code, product_id, snap_date, avg_qty, avg_price = row
                    compacted_time = datetime.combine(snap_date, datetime.min.time().replace(hour=12))
                    cur.execute("""
                        INSERT INTO domae_inventory_snapshots
                        (id, "monitorId", supplier, "productName", unit, "insuranceCode",
                         quantity, price, "productId", "scannedAt")
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """, (
                        _generate_cuid(), mid, supplier, product_name, unit, ins_code,
                        avg_qty, avg_price, product_id, compacted_time,
                    ))

                logger.info("스냅샷 압축 완료 [%s]: %d개 그룹 → 일별 평균", monitor_id, len(groups))

            # 4. 90일 초과 데이터 완전 삭제
            cur.execute("""
                DELETE FROM domae_inventory_snapshots
                WHERE "monitorId" = %s AND "scannedAt" < NOW() - INTERVAL '90 days'
            """, (monitor_id,))

        except Exception as e:
            logger.warning("스냅샷 압축 실패 [%s]: %s", monitor_id, e)

    def _detect_alerts(self, conn, monitor_id: str, keyword: str, new_results: list) -> list:
        """이전 스냅샷과 비교하여 핵심 이벤트만 감지.

        감지 이벤트:
        1. 재입고: 직전 스냅샷 재고 0 → 현재 > 0
        2. 급격한 재고 감소: 직전 스냅샷 대비 30% 이상 감소

        Returns:
            이벤트 dict 리스트:
            [{"type": "restock"|"drop", "supplier": ..., "product_name": ..., ...}]
        """
        cur = conn.cursor()

        # 직전 스냅샷 (현재 저장 직전의 최신 스냅샷)
        cur.execute("""
            SELECT DISTINCT ON (supplier, "productName")
                   supplier, "productName", quantity, price, "productId"
            FROM domae_inventory_snapshots
            WHERE "monitorId" = %s
              AND "scannedAt" < (
                  SELECT MAX("scannedAt") FROM domae_inventory_snapshots
                  WHERE "monitorId" = %s
              )
            ORDER BY supplier, "productName", "scannedAt" DESC
        """, (monitor_id, monitor_id))

        prev_map = {}
        for row in cur.fetchall():
            key = f"{row[0]}|{row[1]}"
            prev_map[key] = {
                "quantity": row[2] or 0,
                "price": row[3] or 0,
                "product_id": row[4] or "",
            }

        if not prev_map:
            return []  # 첫 검색이면 비교 불가

        alerts = []
        for r in new_results:
            key = f"{r['supplier']}|{r['product_name']}"
            prev = prev_map.get(key)
            new_qty = r.get("quantity") or 0
            new_price = r.get("price") or 0

            if prev is None:
                # 신규 제품 — 재입고와 동일 취급 (재고 있을 때만)
                if new_qty > 0:
                    alerts.append({
                        "type": "restock",
                        "supplier": r["supplier"],
                        "product_name": r["product_name"],
                        "product_id": r.get("product_id", ""),
                        "quantity": new_qty,
                        "price": new_price,
                    })
                continue

            prev_qty = prev["quantity"]

            # 1. 재입고: 0 → N
            if prev_qty == 0 and new_qty > 0:
                alerts.append({
                    "type": "restock",
                    "supplier": r["supplier"],
                    "product_name": r["product_name"],
                    "product_id": r.get("product_id", ""),
                    "quantity": new_qty,
                    "price": new_price,
                })

            # 2. 급격한 재고 감소: 30% 이상 감소 (이전 재고 10개 이상일 때만)
            elif prev_qty >= 10 and new_qty < prev_qty:
                drop_pct = (prev_qty - new_qty) / prev_qty
                if drop_pct >= 0.3:
                    alerts.append({
                        "type": "drop",
                        "supplier": r["supplier"],
                        "product_name": r["product_name"],
                        "product_id": r.get("product_id", ""),
                        "old_qty": prev_qty,
                        "new_qty": new_qty,
                        "price": new_price,
                    })

        return alerts

    def search_on_demand(self, job: dict):
        """온디맨드 검색 — 도매상별로 stream_key에 결과를 실시간 전송"""
        monitor_id = job["monitor_id"]
        stream_key = job["stream_key"]
        keywords = job.get("keywords", [])
        requested_suppliers = job.get("suppliers", [])

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
                    supplier_results = self._search_supplier(supplier_name, crawler_cls, cred, keywords)
                    self._redis.lpush(stream_key, json.dumps({
                        "type": "partial",
                        "supplier": supplier_name,
                        "results": supplier_results,
                    }))
                except Exception as e:
                    logger.warning("도매상 검색 실패 [%s]: %s", supplier_name, e)
                    self._redis.lpush(stream_key, json.dumps({
                        "type": "partial",
                        "supplier": supplier_name,
                        "results": [],
                        "error": str(e),
                    }))

            with ThreadPoolExecutor(max_workers=min(len(target_suppliers), 8)) as executor:
                futures = [
                    executor.submit(_search_and_stream, name, cls, cred)
                    for name, (cls, cred) in target_suppliers.items()
                ]
                for future in as_completed(futures):
                    try:
                        future.result(timeout=120)
                    except Exception as e:
                        logger.error("search_on_demand 스레드 에러: %s", e)

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

    def order(self, job: dict):
        """단건 주문 실행 — response_key로 결과 반환"""
        monitor_id = job["monitor_id"]
        response_key = job["response_key"]
        supplier_name = job["supplier"]
        product_id = job["product_id"]
        quantity = job["quantity"]

        conn = self._get_conn()
        try:
            # 1. credentials + telegramChatId 조회
            cur = conn.cursor()
            cur.execute("""
                SELECT m.credentials, m."telegramChatId"
                FROM domae_cloud_monitors m
                WHERE m.id = %s AND m."isActive" = true
            """, (monitor_id,))
            row = cur.fetchone()
            if not row:
                self._redis.lpush(response_key, json.dumps({"success": False, "message": "모니터 없음"}))
                return

            raw_creds = row[0]
            credentials = self._decrypt_creds(raw_creds)
            telegram_chat_id = row[1]

            # 2. 크롤러 로드
            if not self._crawlers_loaded:
                self._load_crawlers(conn)

            cred = credentials.get(supplier_name)
            if not cred:
                self._redis.lpush(response_key, json.dumps({
                    "success": False, "message": f"{supplier_name} 계정 미등록"
                }))
                return

            crawler_cls = self._crawlers.get(supplier_name)
            if not crawler_cls:
                self._redis.lpush(response_key, json.dumps({
                    "success": False, "message": f"{supplier_name} 크롤러 없음"
                }))
                return

            # 3. 주문 실행
            crawler = crawler_cls()
            crawler.login(cred.get("login_id", ""), cred.get("login_pw", ""))
            result = crawler.order(product_id, quantity)

            self._redis.lpush(response_key, json.dumps({
                "success": result.success,
                "order_id": getattr(result, "order_id", None),
                "message": getattr(result, "message", ""),
            }))

            # 텔레그램 알림
            if telegram_chat_id:
                try:
                    from domae_mcp.cloud.notifier import Notifier
                    product_name = job.get("product_name", product_id)
                    if result.success:
                        msg = f"✅ [{supplier_name}] {product_name} {quantity}개 주문 완료"
                    else:
                        msg = f"❌ [{supplier_name}] {product_name} 주문 실패: {getattr(result, 'message', '')}"
                    Notifier.send_telegram(telegram_chat_id, msg)
                except Exception as e:
                    logger.warning("주문 텔레그램 알림 실패: %s", e)

            logger.info("order 완료: monitor=%s supplier=%s success=%s", monitor_id, supplier_name, result.success)

        except Exception as e:
            logger.error("order 실패 [%s]: %s", monitor_id, e, exc_info=True)
            self._redis.lpush(response_key, json.dumps({"success": False, "message": str(e)}))
        finally:
            self._db_pool.putconn(conn)

    def batch_order(self, job: dict):
        """일괄 주문 — DB에 직접 결과 기록 (비동기 배치)"""
        monitor_id = job["monitor_id"]
        batch_id = job["batch_id"]
        items = job.get("items", [])

        conn = self._get_conn()
        try:
            cur = conn.cursor()

            # 1. batch status → processing
            utc_now = datetime.now(timezone.utc)
            cur.execute(
                'UPDATE domae_order_batches SET status = %s WHERE id = %s AND "monitorId" = %s',
                ("processing", batch_id, monitor_id)
            )
            conn.commit()

            # 2. credentials 조회
            cur.execute("""
                SELECT m.credentials, m."telegramChatId"
                FROM domae_cloud_monitors m
                WHERE m.id = %s
            """, (monitor_id,))
            row = cur.fetchone()
            if not row:
                cur.execute(
                    'UPDATE domae_order_batches SET status = %s WHERE id = %s',
                    ("failed", batch_id)
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
            logged_in_crawlers = {}  # 도매상별 로그인 캐시

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

            # 4-2. 사전 실패 항목 기록
            for idx, msg in pre_fail.items():
                item = items[idx]
                fail_count += 1
                utc_now = datetime.now(timezone.utc)
                cur.execute("""
                    INSERT INTO domae_cloud_orders
                    (id, "monitorId", "batchId", supplier, "productName", unit, "insuranceCode",
                     quantity, price, success, "productId", "orderId", message, "orderedAt")
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """, (
                    _generate_cuid(), monitor_id, batch_id, item.get("supplier") or "",
                    item.get("product_name", ""), item.get("unit"), item.get("insurance_code"),
                    item.get("quantity", 1), item.get("price"), False,
                    item.get("product_id"), None, msg, utc_now,
                ))
                cart_item_id = item.get("cart_item_id")
                if cart_item_id:
                    cur.execute(
                        'UPDATE domae_cart_items SET "failedAt" = %s, "failReason" = %s WHERE id = %s',
                        (utc_now, msg, cart_item_id)
                    )
            conn.commit()

            # 4-3. 도매상별 일괄 주문
            for supplier_name, group_items in supplier_groups.items():
                cred = credentials[supplier_name]
                crawler_cls = self._crawlers[supplier_name]

                if supplier_name not in logged_in_crawlers:
                    crawler = crawler_cls()
                    crawler.login(cred.get("login_id", ""), cred.get("login_pw", ""))
                    logged_in_crawlers[supplier_name] = crawler
                crawler = logged_in_crawlers[supplier_name]

                batch_items = [
                    {"product_id": item.get("product_id"), "quantity": item.get("quantity", 1)}
                    for _, item in group_items
                ]

                try:
                    results = crawler.order_batch(batch_items)
                except Exception as e:
                    results = [type('R', (), {'success': False, 'message': str(e), 'order_id': ''})()
                               for _ in group_items]

                # 길이 불일치 방어
                if len(results) != len(group_items):
                    logger.warning("order_batch 반환 길이 불일치: %s expected=%d got=%d",
                                   supplier_name, len(group_items), len(results))
                    from domae_mcp.core.crawlers.base import OrderResult as _OR
                    while len(results) < len(group_items):
                        results.append(_OR(success=False, message="결과 누락"))

                for (idx, item), result in zip(group_items, results):
                    order_success = result.success
                    order_id_val = getattr(result, "order_id", None)
                    order_message = getattr(result, "message", "")
                    order_price = item.get("price")

                    utc_now = datetime.now(timezone.utc)
                    cur.execute("""
                        INSERT INTO domae_cloud_orders
                        (id, "monitorId", "batchId", supplier, "productName", unit, "insuranceCode",
                         quantity, price, success, "productId", "orderId", message, "orderedAt")
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """, (
                        _generate_cuid(), monitor_id, batch_id, supplier_name,
                        item.get("product_name", ""), item.get("unit"), item.get("insurance_code"),
                        item.get("quantity", 1), order_price, order_success,
                        item.get("product_id"), order_id_val, order_message, utc_now,
                    ))

                    if order_success:
                        success_count += 1
                        cart_item_id = item.get("cart_item_id")
                        if cart_item_id:
                            cur.execute('DELETE FROM domae_cart_items WHERE id = %s', (cart_item_id,))
                    else:
                        fail_count += 1
                        cart_item_id = item.get("cart_item_id")
                        if cart_item_id:
                            cur.execute(
                                'UPDATE domae_cart_items SET "failedAt" = %s, "failReason" = %s WHERE id = %s',
                                (utc_now, order_message, cart_item_id)
                            )

                # batch 카운트 업데이트
                cur.execute("""
                    UPDATE domae_order_batches
                    SET "successCount" = %s, "failCount" = %s
                    WHERE id = %s
                """, (success_count, fail_count, batch_id))
                conn.commit()

                time.sleep(1)  # 도매상 간 딜레이

            # 5. batch 완료
            utc_now = datetime.now(timezone.utc)
            cur.execute("""
                UPDATE domae_order_batches
                SET status = %s, "completedAt" = %s
                WHERE id = %s
            """, ("completed", utc_now, batch_id))
            conn.commit()

            # 6. 텔레그램 알림
            if telegram_chat_id:
                try:
                    from domae_mcp.cloud.notifier import Notifier
                    msg = f"📦 도매 일괄주문 완료\n\n성공: {success_count}건\n실패: {fail_count}건"
                    Notifier.send_telegram(telegram_chat_id, msg)
                except Exception as e:
                    logger.warning("텔레그램 알림 실패: %s", e)

            logger.info("batch_order 완료: batch=%s success=%d fail=%d", batch_id, success_count, fail_count)

        except Exception as e:
            conn.rollback()
            logger.error("batch_order 실패 [%s]: %s", batch_id, e, exc_info=True)
            try:
                cur = conn.cursor()
                cur.execute('UPDATE domae_order_batches SET status = %s WHERE id = %s', ("failed", batch_id))
                conn.commit()
            except Exception:
                pass
        finally:
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

            filled = 0
            details = []

            for sup_info in suppliers_info:
                if filled >= remaining_qty:
                    break

                supplier_name = sup_info["supplier"]
                product_id_val = sup_info["product_id"]
                need = remaining_qty - filled

                cred = credentials.get(supplier_name)
                if not cred:
                    details.append({"supplier": supplier_name, "quantity": 0, "success": False, "message": "계정 미등록"})
                    continue

                crawler_cls = self._crawlers.get(supplier_name)
                if not crawler_cls:
                    details.append({"supplier": supplier_name, "quantity": 0, "success": False, "message": "크롤러 없음"})
                    continue

                try:
                    crawler = crawler_cls()
                    crawler.login(cred.get("login_id", ""), cred.get("login_pw", ""))

                    # 재고 확인
                    scanned_at = datetime.now(timezone.utc).replace(tzinfo=None)
                    search_results = crawler.search(product_id_val)
                    available = 0
                    for sr in search_results:
                        if sr.product_id == product_id_val and sr.quantity and sr.quantity > 0:
                            available = sr.quantity
                            break

                    if available == 0:
                        details.append({"supplier": supplier_name, "quantity": 0, "success": False, "message": "재고 없음"})
                        # 로그 기록
                        utc_now = datetime.now(timezone.utc)
                        cur.execute("""
                            INSERT INTO domae_urgent_logs
                            (id, "urgentOrderId", supplier, "orderedQuantity", success, message, "scannedAt", "orderedAt")
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                        """, (_generate_cuid(), urgent_order_id, supplier_name, 0, False, "재고 없음", scanned_at, utc_now))
                        conn.commit()
                        continue

                    # 주문 실행
                    order_qty = min(need, available)
                    result = crawler.order(product_id_val, order_qty)

                    utc_now = datetime.now(timezone.utc)
                    cur.execute("""
                        INSERT INTO domae_urgent_logs
                        (id, "urgentOrderId", supplier, "orderedQuantity", success, message, "scannedAt", "orderedAt")
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    """, (_generate_cuid(), urgent_order_id, supplier_name, order_qty, result.success,
                          getattr(result, "message", ""), scanned_at, utc_now))

                    if result.success:
                        filled += order_qty
                        details.append({"supplier": supplier_name, "quantity": order_qty, "success": True,
                                        "message": getattr(result, "message", "주문 완료")})
                    else:
                        details.append({"supplier": supplier_name, "quantity": 0, "success": False,
                                        "message": getattr(result, "message", "주문 실패")})
                    conn.commit()

                except Exception as e:
                    details.append({"supplier": supplier_name, "quantity": 0, "success": False, "message": str(e)})
                    logger.warning("urgent immediate [%s/%s]: %s", supplier_name, urgent_order_id, e)

                time.sleep(0.5)

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
                    utc_now = datetime.now(timezone.utc)
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
            crawler.login(cred.get("login_id", ""), cred.get("login_pw", ""))

            # 제품명/가격 조회
            product_name = product_id
            price = 0
            try:
                search_results = crawler.search(product_id)
                for sr in search_results:
                    if sr.product_id == product_id:
                        product_name = sr.product_name
                        price = sr.price or 0
                        break
            except Exception:
                pass

            result = crawler.order(product_id, quantity)

            Notifier.send_order_result(
                chat_id, message_id, original_text,
                product_name, supplier_name, quantity, price,
                success=result.success,
                error_msg=getattr(result, "message", ""),
            )

            logger.info(
                "telegram_order 완료: supplier=%s product=%s qty=%d success=%s",
                supplier_name, product_id, quantity, result.success,
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

            for supplier_name, product_id_val in suppliers:
                if filled_this_round >= remaining:
                    break

                cred = credentials.get(supplier_name)
                if not cred:
                    continue

                crawler_cls = self._crawlers.get(supplier_name)
                if not crawler_cls:
                    continue

                try:
                    crawler = crawler_cls()
                    crawler.login(cred.get("login_id", ""), cred.get("login_pw", ""))

                    scanned_at = datetime.now(timezone.utc).replace(tzinfo=None)
                    search_results = crawler.search(product_id_val)
                    available = 0
                    for sr in search_results:
                        if sr.product_id == product_id_val and sr.quantity and sr.quantity > 0:
                            available = sr.quantity
                            break

                    if available == 0:
                        continue

                    order_qty = min(remaining - filled_this_round, available)
                    result = crawler.order(product_id_val, order_qty)

                    utc_now = datetime.now(timezone.utc)
                    cur.execute("""
                        INSERT INTO domae_urgent_logs
                        (id, "urgentOrderId", supplier, "orderedQuantity", success, message, "scannedAt", "orderedAt")
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    """, (_generate_cuid(), uo_id, supplier_name, order_qty, result.success,
                          getattr(result, "message", ""), scanned_at, utc_now))

                    if result.success:
                        filled_this_round += order_qty

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

                    conn.commit()
                    time.sleep(0.5)

                except Exception as e:
                    logger.warning("urgent process [%s/%s]: %s", uo_id, supplier_name, e)

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
                    utc_now = datetime.now(timezone.utc)
                    cur.execute(
                        'UPDATE domae_urgent_orders SET active = false, "completedAt" = %s WHERE id = %s',
                        (utc_now, uo_id)
                    )

                conn.commit()

        logger.info("긴급주문 처리 완료: monitor=%s, %d건", monitor_id, len(urgent_orders))
