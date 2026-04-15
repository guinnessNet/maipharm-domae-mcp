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
from datetime import datetime, timedelta, timezone

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
            # supplier별 (keyword) 실패 정보 — 크롤러 에러 vs 정상 empty 구분
            # failed_lookups[supplier] = set of keywords that errored (로그인 실패 시 전 키워드)
            failed_lookups: dict = {}
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
                        ret = future.result(timeout=120)
                        all_results.extend(ret["results"])
                        if not ret["login_ok"]:
                            failed_lookups[supplier] = set(products)  # 전 키워드 무응답
                        elif ret["failed_keywords"]:
                            failed_lookups[supplier] = ret["failed_keywords"]
                    except Exception as e:
                        failed_lookups[supplier] = set(products)
                        logger.error("도매 검색 실패 [%s]: %s", supplier, e)

            # 4. 변동 감지 — 저장 BEFORE (prev baseline이 현재 save에 덮이지 않도록)
            all_alerts = []
            for keyword in products:
                keyword_results = [r for r in all_results if r["keyword"] == keyword]
                # 이 키워드에서 에러난 supplier 집합
                failed_for_kw = {
                    sup for sup, kws in failed_lookups.items() if keyword in kws
                }
                if keyword_results or failed_for_kw:
                    alerts = self._detect_alerts(
                        conn, monitor_id, keyword, keyword_results, failed_for_kw
                    )
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
                                total_value=alert.get("total_value", 0),
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
        """검색 결과 DB 저장 (스냅샷 누적 + 24h 정리)"""
        cur = conn.cursor()
        utc_now = datetime.now(timezone.utc).replace(tzinfo=None)

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
        failed_suppliers: set = None,
    ) -> list:
        """이전 스냅샷과 비교하여 핵심 이벤트만 감지.

        감지 이벤트:
        1. 재입고: 직전 스냅샷 재고 0 → 현재 > 0
        2. 급격한 재고 감소: 직전 스냅샷 대비 30% 이상 감소 (전 도매 합산)

        Args:
            failed_suppliers: 이번 scan에서 에러난 supplier 집합. 이들만 "변화없음" 가정 적용.
                             정상 empty 응답 supplier는 new_qty=0으로 취급 (실제 품절 가능).
        """
        failed_suppliers = failed_suppliers or set()
        cur = conn.cursor()

        # 현재 키워드의 제품명 집합 (cross-keyword 오염 방지)
        # prev 쿼리를 이 제품명으로만 한정. new_results가 비어있을 때도 failed_suppliers 평가 위해
        # 가능한 제품명을 직전 스냅샷에서 추론하되, 그 경우엔 prev_map 전체를 사용하는 게 아니라
        # failed_suppliers만 있고 new_results가 비었으면 아예 비교 불가 → 빈 알림 반환.
        product_names = list({r["product_name"] for r in new_results})
        if not product_names:
            return []

        cur.execute("""
            SELECT DISTINCT ON (supplier, "productName")
                   supplier, "productName", quantity, price, "productId"
            FROM domae_inventory_snapshots
            WHERE "monitorId" = %s
              AND "productName" = ANY(%s)
            ORDER BY supplier, "productName", "scannedAt" DESC
        """, (monitor_id, product_names))

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

        # new_results를 (supplier, product_name) 인덱스로 변환
        new_map = {}
        for r in new_results:
            key = f"{r['supplier']}|{r['product_name']}"
            new_map[key] = {
                "quantity": r.get("quantity") or 0,
                "price": r.get("price") or 0,
                "product_id": r.get("product_id", ""),
            }

        # 제품별 supplier 집합 (prev ∪ new)
        product_suppliers: dict = {}
        for key in set(prev_map.keys()) | set(new_map.keys()):
            supplier, pname = key.split("|", 1)
            product_suppliers.setdefault(pname, set()).add(supplier)

        prev_product_totals: dict = {}
        new_product_totals: dict = {}
        for pname, suppliers in product_suppliers.items():
            prev_total = 0
            new_total = 0
            max_price = 0
            product_id = ""
            total_value = 0  # Σ(price_i × qty_i) — 진짜 잔여금액
            for sup in suppliers:
                key = f"{sup}|{pname}"
                prev_val = prev_map.get(key)
                new_val = new_map.get(key)

                prev_qty = prev_val["quantity"] if prev_val else 0

                if new_val is not None:
                    # 이번 scan에서 실제 응답 받음 — 그 값 사용 (empty=0 포함)
                    new_qty = new_val["quantity"]
                    price = new_val["price"]
                    if new_val.get("product_id"):
                        product_id = new_val["product_id"]
                elif sup in failed_suppliers:
                    # 크롤러 에러 — prev 값 유지 (변화없음 가정)
                    new_qty = prev_qty
                    price = prev_val["price"] if prev_val else 0
                    if prev_val and prev_val.get("product_id"):
                        product_id = prev_val["product_id"]
                else:
                    # 정상 응답했지만 이 supplier+product 조합이 없음 = 실제로 해당 supplier에서 사라짐
                    new_qty = 0
                    price = prev_val["price"] if prev_val else 0

                prev_total += prev_qty
                new_total += new_qty
                total_value += price * new_qty
                if price > max_price:
                    max_price = price

            prev_product_totals[pname] = prev_total
            new_product_totals[pname] = {
                "qty": new_total,
                "price": max_price,
                "product_id": product_id,
                "total_value": total_value,
            }

        # 이미 합산 drop 알림을 생성한 제품 추적
        drop_alerted_products = set()

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

            # 1. 재입고: 0 → N (도매별 개별 유지)
            if prev_qty == 0 and new_qty > 0:
                alerts.append({
                    "type": "restock",
                    "supplier": r["supplier"],
                    "product_name": r["product_name"],
                    "product_id": r.get("product_id", ""),
                    "quantity": new_qty,
                    "price": new_price,
                })

            # 2. 재고 감소: 전체 도매 합산 비교 → 1건만 알림
            pname = r["product_name"]
            if pname not in drop_alerted_products:
                total_prev = prev_product_totals.get(pname, 0)
                total_new = new_product_totals.get(pname, {}).get("qty", 0)
                if total_prev >= 10 and total_new < total_prev:
                    drop_pct = (total_prev - total_new) / total_prev
                    if drop_pct >= 0.3:
                        drop_alerted_products.add(pname)
                        agg = new_product_totals[pname]
                        alerts.append({
                            "type": "drop",
                            "supplier": "전체",
                            "product_name": pname,
                            "product_id": agg.get("product_id", ""),
                            "old_qty": total_prev,
                            "new_qty": total_new,
                            "price": agg["price"],
                            "total_value": agg.get("total_value", 0),
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
        def _finalize(success: bool, order_id: str | None, message: str):
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
                                message = %s
                            WHERE id = %s
                        """, (success, order_id, message, db_order_id))
                        # quick-order 단건 batch도 함께 마감 (서버 timeout/disconnect 대비)
                        # — 서버가 즉시 마감했을 수도 있으나, UPDATE는 멱등이므로 중복 안전
                        if db_batch_id:
                            utc_now = datetime.now(timezone.utc).replace(tzinfo=None)
                            upd_cur.execute("""
                                UPDATE domae_order_batches
                                SET status = %s,
                                    "successCount" = %s,
                                    "failCount" = %s,
                                    "completedAt" = %s
                                WHERE id = %s
                            """, ("completed", 1 if success else 0, 0 if success else 1, utc_now, db_batch_id))
                        upd_conn.commit()
                        logger.info("order DB finalize: dbOrderId=%s dbBatchId=%s success=%s", db_order_id, db_batch_id, success)
                    finally:
                        try:
                            self._db_pool.putconn(upd_conn)
                        except Exception:
                            pass
                except Exception as e:
                    logger.error("order DB finalize 실패 [dbOrderId=%s]: %s", db_order_id, e, exc_info=True)

            # 2) response_key lpush + TTL 설정 (서버 BRPOP용)
            payload = {"success": success, "order_id": order_id, "message": message}
            try:
                self._redis.lpush(response_key, json.dumps(payload))
                # TTL은 lpush 직후에 설정해야 key가 존재해서 적용됨
                # 서버가 BRPOP으로 즉시 consume하면 key 자동 삭제, timeout 시엔 TTL로 정리
                self._redis.expire(response_key, response_key_ttl)
            except Exception as e:
                logger.warning("order response_key lpush/expire 실패: %s", e)

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
            crawler.login(cred.get("login_id", ""), cred.get("login_pw", ""))
            result = crawler.order(product_id, quantity)

            _finalize(
                success=bool(result.success),
                order_id=getattr(result, "order_id", None),
                message=getattr(result, "message", "") or "",
            )

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
            _finalize(False, None, str(e))
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
            utc_now = datetime.now(timezone.utc).replace(tzinfo=None)
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
            success_lines = []  # 텔레그램 알림용
            fail_lines = []
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
                fail_lines.append(
                    f" · [{item.get('supplier', '?')}] {item.get('product_name', '')} ×{item.get('quantity', 1)} — {msg}"
                )
                utc_now = datetime.now(timezone.utc).replace(tzinfo=None)
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
                    # SUPPORTS_CART_SYNC 모드: 장바구니 = 재고 선점 상태이므로 누락 재담기 없이 바로 위임.
                    # 잔존 여부 확인 + 전송은 모두 크롤러 내부(order_batch)에서 처리한다.
                    # 동시 cart_sync 작업과 경합 방지를 위해 도매상별 락만 획득.
                    if getattr(crawler_cls, "SUPPORTS_CART_SYNC", False):
                        lock_key = f"domae:cart:lock:{monitor_id}:{supplier_name}"
                        lock_acquired = self._redis.set(lock_key, "1", nx=True, ex=120)
                        if not lock_acquired:
                            logger.warning("batch_order: %s 장바구니 락 획득 실패, 대기 후 재시도", supplier_name)
                            time.sleep(3)
                            lock_acquired = self._redis.set(lock_key, "1", nx=True, ex=120)

                        try:
                            results = crawler.order_batch(batch_items)
                        finally:
                            if lock_acquired:
                                self._redis.delete(lock_key)
                    else:
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
                NO_RETRY_KEYWORDS = ["재고 부족", "로그인 실패", "계정 미등록", "크롤러 없음", "미지원"]
                retryable = []
                for entry in failed:
                    msg = getattr(entry[2], "message", "")
                    if any(kw in msg for kw in NO_RETRY_KEYWORDS):
                        continue
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
                            retry_result = crawler.order(pid, qty)
                            if retry_result.success:
                                logger.info("batch_order 재시도 성공: %s pid=%s", supplier_name, pid)
                                succeeded.append((idx, item, retry_result))
                            else:
                                still_failed.append((idx, item, retry_result))
                        except Exception as e:
                            logger.warning("batch_order 재시도 실패: %s pid=%s err=%s", supplier_name, pid, e)
                            still_failed.append((idx, item, orig_result))
                    # failed를 재시도 불가 + 재시도 실패로 재구성
                    failed = [e for e in failed if any(kw in getattr(e[2], "message", "") for kw in NO_RETRY_KEYWORDS)] + still_failed

                # ── 결과 DB 기록 ──
                for idx, item, result in succeeded:
                    order_id_val = getattr(result, "order_id", None)
                    order_message = getattr(result, "message", "")
                    order_price = item.get("price")
                    utc_now = datetime.now(timezone.utc).replace(tzinfo=None)
                    cur.execute("""
                        INSERT INTO domae_cloud_orders
                        (id, "monitorId", "batchId", supplier, "productName", unit, "insuranceCode",
                         quantity, price, success, "productId", "orderId", message, "orderedAt")
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """, (
                        _generate_cuid(), monitor_id, batch_id, supplier_name,
                        item.get("product_name", ""), item.get("unit"), item.get("insurance_code"),
                        item.get("quantity", 1), order_price, True,
                        item.get("product_id"), order_id_val, order_message, utc_now,
                    ))
                    success_count += 1
                    _tg_line = f" · [{supplier_name}] {item.get('product_name', '')} ×{item.get('quantity', 1)}"
                    success_lines.append(_tg_line)
                    cart_item_id = item.get("cart_item_id")
                    if cart_item_id:
                        cur.execute('DELETE FROM domae_cart_items WHERE id = %s', (cart_item_id,))

                for idx, item, result in failed:
                    order_message = getattr(result, "message", "")
                    order_price = item.get("price")
                    utc_now = datetime.now(timezone.utc).replace(tzinfo=None)
                    cur.execute("""
                        INSERT INTO domae_cloud_orders
                        (id, "monitorId", "batchId", supplier, "productName", unit, "insuranceCode",
                         quantity, price, success, "productId", "orderId", message, "orderedAt")
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """, (
                        _generate_cuid(), monitor_id, batch_id, supplier_name,
                        item.get("product_name", ""), item.get("unit"), item.get("insurance_code"),
                        item.get("quantity", 1), order_price, False,
                        item.get("product_id"), None, order_message, utc_now,
                    ))
                    fail_count += 1
                    _tg_line = f" · [{supplier_name}] {item.get('product_name', '')} ×{item.get('quantity', 1)}"
                    fail_lines.append(_tg_line + (f" — {order_message}" if order_message else ""))
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
            utc_now = datetime.now(timezone.utc).replace(tzinfo=None)
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
                    parts = ["📦 도매 일괄주문 완료\n"]
                    if success_lines:
                        parts.append(f"✅ 성공 {success_count}건")
                        parts.extend(success_lines[:10])
                        if len(success_lines) > 10:
                            parts.append(f" ... 외 {len(success_lines) - 10}건")
                    if fail_lines:
                        if success_lines:
                            parts.append("")
                        parts.append(f"❌ 실패 {fail_count}건")
                        parts.extend(fail_lines[:10])
                        if len(fail_lines) > 10:
                            parts.append(f" ... 외 {len(fail_lines) - 10}건")
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
                cur.execute('UPDATE domae_order_batches SET status = %s WHERE id = %s', ("failed", batch_id))
                conn.commit()
            except Exception:
                pass
            # 부분 성공이라도 텔레그램 알림 발송
            if telegram_chat_id and (success_lines or fail_lines):
                try:
                    from domae_mcp.cloud.notifier import Notifier
                    parts = [f"📦 도매 일괄주문 오류 (일부 처리됨)\n"]
                    if success_lines:
                        parts.append(f"✅ 성공 {success_count}건")
                        parts.extend(success_lines[:10])
                    if fail_lines:
                        if success_lines:
                            parts.append("")
                        parts.append(f"❌ 실패 {fail_count}건")
                        parts.extend(fail_lines[:10])
                    parts.append(f"\n⚠️ 오류: {str(e)[:100]}")
                    Notifier.send_telegram(telegram_chat_id, "\n".join(parts))
                except Exception:
                    pass
        finally:
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

        try:
            cur = conn.cursor()

            # 1. batch status → processing
            utc_now = datetime.now(timezone.utc).replace(tzinfo=None)
            cur.execute(
                'UPDATE domae_order_batches SET status = %s WHERE id = %s AND "monitorId" = %s',
                ("processing", batch_id, monitor_id)
            )
            conn.commit()

            # 2. credentials + telegramChatId 조회 (isActive 체크 포함)
            cur.execute("""
                SELECT m.credentials, m."telegramChatId"
                FROM domae_cloud_monitors m
                WHERE m.id = %s AND m."isActive" = true
            """, (monitor_id,))
            row = cur.fetchone()
            if not row:
                self._update_auto_order_log(conn, monitor_id, batch_id, "failed", "모니터 없음 또는 비활성")
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

            # 4. 사전 검증
            cred = credentials.get(supplier_name)
            if not cred:
                self._update_auto_order_log(conn, monitor_id, batch_id, "failed", f"{supplier_name} 계정 미등록")
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
                cur.execute('UPDATE domae_order_batches SET status = %s WHERE id = %s', ("failed", batch_id))
                conn.commit()
                if telegram_chat_id:
                    self._send_auto_order_telegram(telegram_chat_id, supplier_name, [], items,
                                                   global_error=f"{supplier_name} 크롤러 없음",
                                                   scheduled_at=scheduled_at)
                return

            # 5. 로그인 + 주문 실행
            crawler = crawler_cls()
            crawler.login(cred.get("login_id", ""), cred.get("login_pw", ""))

            batch_items = [
                {"product_id": item.get("product_id"), "quantity": item.get("quantity", 1)}
                for item in items
            ]

            # SUPPORTS_CART_SYNC 도매상은 동시 cart_sync 작업과 경합 방지를 위해 락 획득
            ao_lock_key = None
            ao_lock_acquired = False
            if getattr(crawler_cls, "SUPPORTS_CART_SYNC", False):
                ao_lock_key = f"domae:cart:lock:{monitor_id}:{supplier_name}"
                ao_lock_acquired = self._redis.set(ao_lock_key, "1", nx=True, ex=120)
                if not ao_lock_acquired:
                    logger.warning("auto_order: %s 장바구니 락 획득 실패, 대기 후 재시도", supplier_name)
                    time.sleep(3)
                    ao_lock_acquired = self._redis.set(ao_lock_key, "1", nx=True, ex=120)

            try:
                results = crawler.order_batch(batch_items)
            except Exception as e:
                results = [type('R', (), {'success': False, 'message': str(e), 'order_id': ''})()
                           for _ in items]
            finally:
                if ao_lock_key and ao_lock_acquired:
                    self._redis.delete(ao_lock_key)

            # 길이 불일치 방어
            if len(results) != len(items):
                logger.warning("auto_order order_batch 반환 길이 불일치: %s expected=%d got=%d",
                               supplier_name, len(items), len(results))
                from domae_mcp.core.crawlers.base import OrderResult as _OR
                while len(results) < len(items):
                    results.append(_OR(success=False, message="결과 누락"))

            success_count = 0
            fail_count = 0

            for item, result in zip(items, results):
                order_success = result.success
                order_id_val = getattr(result, "order_id", None)
                order_message = getattr(result, "message", "")
                order_price = item.get("price")

                utc_now = datetime.now(timezone.utc).replace(tzinfo=None)
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
                    success_items.append(item)
                    cart_item_id = item.get("cart_item_id")
                    if cart_item_id:
                        cur.execute('DELETE FROM domae_cart_items WHERE id = %s', (cart_item_id,))
                else:
                    fail_count += 1
                    failed_items.append({**item, "message": order_message})
                    cart_item_id = item.get("cart_item_id")
                    if cart_item_id:
                        cur.execute(
                            'UPDATE domae_cart_items SET "failedAt" = %s, "failReason" = %s WHERE id = %s',
                            (utc_now, order_message, cart_item_id)
                        )

            # 6. batch 완료
            utc_now = datetime.now(timezone.utc).replace(tzinfo=None)
            cur.execute("""
                UPDATE domae_order_batches
                SET status = %s, "successCount" = %s, "failCount" = %s, "completedAt" = %s
                WHERE id = %s
            """, ("completed", success_count, fail_count, utc_now, batch_id))
            conn.commit()

            # 7. DomaeAutoOrderLog 상태 업데이트
            if fail_count == 0:
                log_status = "success"
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
                    scheduled_at=scheduled_at,
                )

            # 9. SSE 결과 알림 (Redis publish)
            try:
                self._redis.publish(f"domae:notifications:{monitor_id}", json.dumps({
                    "type": "auto_order_result",
                    "supplier": supplier_name,
                    "status": "success" if not failed_items else "partial_fail",
                    "count": len(success_items),
                    "totalPrice": sum(i.get("price", 0) * i.get("quantity", 0) for i in success_items if i.get("price")),
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
                cur.execute('UPDATE domae_order_batches SET status = %s WHERE id = %s', ("failed", batch_id))
                conn.commit()
            except Exception:
                pass
            self._update_auto_order_log(conn, monitor_id, batch_id, "failed", str(e)[:200])
            # 부분 성공이라도 텔레그램 알림
            if telegram_chat_id and (success_items or failed_items):
                self._send_auto_order_telegram(
                    telegram_chat_id, supplier_name, success_items, failed_items,
                    conn=conn, monitor_id=monitor_id, credentials=credentials,
                    scheduled_at=scheduled_at,
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
                                  scheduled_at: str = ""):
        """자동주문 결과 텔레그램 알림 전송.

        실패 품목이 있으면 다른 도매에서 대체 검색 후 인라인 버튼으로 표시.
        """
        try:
            from domae_mcp.cloud.notifier import Notifier
            # scheduled_at은 이미 KST 기준 마감시간 (예: "14:00")
            if scheduled_at:
                now_str = scheduled_at
            else:
                KST = timezone(timedelta(hours=9))
                now_str = datetime.now(KST).strftime("%H:%M")

            if global_error:
                # 전체 실패 (계정 미등록, 크롤러 없음 등)
                msg = f"❌ 자동주문 실패 ({supplier}, {now_str})\n\n{global_error}\n\n수동으로 확인해주세요."
                Notifier.send_telegram(chat_id, msg)
                return

            if success_items and not failed_items:
                # 전체 성공
                lines = [f"✅ 자동주문 완료 ({supplier}, {now_str})\n", "주문 내역:"]
                total_price = 0
                for item in success_items:
                    qty = item.get("quantity", 1)
                    price = item.get("price", 0) or 0
                    line_total = price * qty
                    total_price += line_total
                    lines.append(f"• {item.get('product_name', '')} — {qty}개 — {line_total:,}원")
                lines.append(f"\n총 {len(success_items)}건, {total_price:,}원 주문 완료")
                Notifier.send_telegram(chat_id, "\n".join(lines))
                return

            # 실패 품목 있음 → 대체 도매 검색
            inline_keyboard = []
            if failed_items and credentials and monitor_id:
                available_suppliers = [
                    s for s in credentials.keys()
                    if s != supplier and self._crawlers.get(s)
                ]
                for item in failed_items[:5]:  # 최대 5개 품목만 대체 검색
                    alt_results = self._search_alternatives(
                        item.get("product_name", ""), available_suppliers, credentials
                    )
                    if alt_results:
                        row = []
                        for alt in alt_results[:3]:  # 도매당 최대 3개
                            price_str = f" {alt['price']:,}원" if alt.get("price") else ""
                            mid = Notifier._sanitize_cb_field(monitor_id, 8)
                            sup = Notifier._sanitize_cb_field(alt["supplier"], 10)
                            pid = Notifier._sanitize_cb_field(alt["product_id"], 16)
                            qty = item.get("quantity", 1)
                            cb_data = f"AO:{mid}:{sup}:{pid}:{qty}"
                            if len(cb_data.encode("utf-8")) <= 64:
                                row.append({
                                    "text": f"{alt['supplier']}{price_str}",
                                    "callback_data": cb_data,
                                })
                        if row:
                            inline_keyboard.append(row)

            reply_markup = {"inline_keyboard": inline_keyboard} if inline_keyboard else None

            if not success_items and failed_items:
                # 전체 실패
                lines = [f"❌ 자동주문 실패 ({supplier}, {now_str})\n"]
                for item in failed_items[:10]:
                    qty = item.get("quantity", 1)
                    reason = item.get("message", "주문 실패")
                    lines.append(f"• {item.get('product_name', '')} {qty}개 — {reason}")
                if len(failed_items) > 10:
                    lines.append(f" ... 외 {len(failed_items) - 10}건")
                if inline_keyboard:
                    lines.append("\n대체 도매에서 주문하려면 아래 버튼을 누르세요:")
                else:
                    lines.append("\n수동으로 확인해주세요.")
                Notifier.send_telegram(chat_id, "\n".join(lines), reply_markup=reply_markup)

            else:
                # 부분 실패
                lines = [f"⚠️ 자동주문 부분 완료 ({supplier}, {now_str})\n"]
                lines.append("✅ 성공:")
                total_price = 0
                for item in success_items[:10]:
                    qty = item.get("quantity", 1)
                    price = item.get("price", 0) or 0
                    line_total = price * qty
                    total_price += line_total
                    lines.append(f"• {item.get('product_name', '')} — {qty}개 — {line_total:,}원")
                if len(success_items) > 10:
                    lines.append(f" ... 외 {len(success_items) - 10}건")

                lines.append("\n❌ 실패:")
                for item in failed_items[:10]:
                    qty = item.get("quantity", 1)
                    reason = item.get("message", "주문 실패")
                    lines.append(f"• {item.get('product_name', '')} — {reason}")
                if len(failed_items) > 10:
                    lines.append(f" ... 외 {len(failed_items) - 10}건")

                if inline_keyboard:
                    lines.append("\n대체 도매에서 주문하려면 아래 버튼을 누르세요:")
                else:
                    lines.append("\n수동으로 확인해주세요.")
                Notifier.send_telegram(chat_id, "\n".join(lines), reply_markup=reply_markup)

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

        with ThreadPoolExecutor(max_workers=min(len(available_suppliers), 5)) as executor:
            futures = {executor.submit(_search_one, sup): sup for sup in available_suppliers}
            for future in as_completed(futures):
                try:
                    result = future.result(timeout=60)
                    if result:
                        results.append(result)
                except Exception:
                    pass

        return results

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

            if result.success:
                # 성공 → 메시지 편집: 버튼 제거 + 완료 표시
                Notifier.send_order_result(
                    chat_id, message_id, original_text,
                    product_name, supplier_name, quantity, price,
                    success=True,
                )
                # DB 기록
                utc_now = datetime.now(timezone.utc).replace(tzinfo=None)
                cur.execute("""
                    INSERT INTO domae_cloud_orders
                    (id, "monitorId", supplier, "productName",
                     quantity, price, success, "productId", "orderId", message, "orderedAt")
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """, (
                    _generate_cuid(), monitor_id, supplier_name,
                    product_name, quantity, price, True,
                    product_id, getattr(result, "order_id", None),
                    getattr(result, "message", ""), utc_now,
                ))
                conn.commit()
            else:
                # 실패 → 남은 도매에서 검색 → 인라인 버튼 재표시
                all_tried = list(set(tried_suppliers + [supplier_name]))
                remaining_suppliers = [
                    s for s in credentials.keys()
                    if s not in all_tried and self._crawlers.get(s)
                ]

                inline_keyboard = []
                if remaining_suppliers:
                    alt_results = self._search_alternatives(
                        product_name if product_name != product_id else product_id,
                        remaining_suppliers, credentials,
                    )
                    if alt_results:
                        row_btns = []
                        for alt in alt_results[:3]:
                            price_str = f" {alt['price']:,}원" if alt.get("price") else ""
                            mid = Notifier._sanitize_cb_field(monitor_id, 8)
                            sup = Notifier._sanitize_cb_field(alt["supplier"], 10)
                            pid = Notifier._sanitize_cb_field(alt["product_id"], 16)
                            cb_data = f"AO:{mid}:{sup}:{pid}:{quantity}"
                            if len(cb_data.encode("utf-8")) <= 64:
                                row_btns.append({
                                    "text": f"{alt['supplier']}{price_str}",
                                    "callback_data": cb_data,
                                })
                        if row_btns:
                            inline_keyboard.append(row_btns)

                error_msg = getattr(result, "message", "주문 실패")
                fail_text = f"\n\n❌ {supplier_name} 주문 실패: {error_msg}"

                if inline_keyboard:
                    fail_text += "\n\n다른 도매에서 주문하려면 아래 버튼을 누르세요:"
                    reply_markup = {"inline_keyboard": inline_keyboard}
                else:
                    fail_text += "\n\n모든 도매 주문 실패 — 수동으로 확인해주세요."
                    reply_markup = None

                if message_id:
                    updated_text = original_text + fail_text
                    Notifier.edit_message(chat_id, message_id, updated_text, reply_markup=reply_markup)
                else:
                    Notifier.send_telegram(chat_id, fail_text.strip(), reply_markup=reply_markup)

                # DB 기록 (실패)
                utc_now = datetime.now(timezone.utc).replace(tzinfo=None)
                cur.execute("""
                    INSERT INTO domae_cloud_orders
                    (id, "monitorId", supplier, "productName",
                     quantity, price, success, "productId", message, "orderedAt")
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """, (
                    _generate_cuid(), monitor_id, supplier_name,
                    product_name, quantity, price, False,
                    product_id, error_msg, utc_now,
                ))
                conn.commit()

            logger.info(
                "auto_order_retry 완료: supplier=%s product=%s qty=%d success=%s",
                supplier_name, product_id, quantity, result.success,
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
                    crawler.login(cred.get("login_id", ""), cred.get("login_pw", ""))

                    # 재고 확인
                    scanned_at = datetime.now(timezone.utc).replace(tzinfo=None)
                    if first_scanned_at is None:
                        first_scanned_at = scanned_at
                    search_results = crawler.search(product_id_val)
                    available = 0
                    for sr in search_results:
                        if sr.product_id == product_id_val and sr.quantity and sr.quantity > 0:
                            available = sr.quantity
                            break

                    if available == 0:
                        details.append({"supplier": supplier_name, "quantity": 0, "success": False, "message": "재고 없음"})
                        supplier_results[supplier_name] = {"qty": 0}
                        continue

                    # 주문 실행
                    order_qty = min(need, available)
                    result = crawler.order(product_id_val, order_qty)

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
            lock_key = f"domae:cart:lock:{monitor_id}:{supplier_name}"
            lock_acquired = self._redis.set(lock_key, "1", nx=True, ex=30)
            if not lock_acquired:
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
                    found = any(c["pc"] == product_id for c in cart)
                    if found:
                        success = True
                        message = "장바구니 동기화 완료"
                    else:
                        success = False
                        message = "장바구니 담기 실패 (도매몰 거부)"

                elif action == "cart_sync_update":
                    crawler.update_cart_qty(product_id, quantity, price=price)
                    cart = crawler._get_cart_items()
                    found = any(c["pc"] == product_id for c in cart)
                    success = found
                    message = "수량 변경 동기화 완료" if found else "수량 변경 실패"

                elif action == "cart_sync_remove":
                    crawler.remove_from_cart(product_id)
                    cart = crawler._get_cart_items()
                    still_exists = any(c["pc"] == product_id for c in cart)
                    success = not still_exists
                    message = "삭제 동기화 완료" if success else "삭제 실패 (항목 잔존)"

                # DB 동기화 상태 업데이트
                status = "synced" if success else "failed"
                self._cart_sync_update_status(conn, cart_item_id, status, message if not success else None)

                self._cart_sync_respond(response_key, success, message)
                logger.info("cart_sync %s: monitor=%s supplier=%s pid=%s → %s",
                            action, monitor_id, supplier_name, product_id, message)
            finally:
                self._redis.delete(lock_key)

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

            result = crawler.order(product_id, quantity)
            logger.info(
                "telegram_order 완료: supplier=%s product=%s(%s) qty=%d success=%s msg=%s",
                supplier_name, product_id, product_name, quantity,
                result.success, getattr(result, "message", ""),
            )

            Notifier.send_order_result(
                chat_id, message_id, original_text,
                product_name, supplier_name, quantity, price,
                success=result.success,
                error_msg=getattr(result, "message", ""),
            )

            # DB 주문 기록 저장 (배치 생성 → 주문 연결)
            utc_now = datetime.now(timezone.utc).replace(tzinfo=None)
            cur = conn.cursor()
            batch_id = _generate_cuid()
            cur.execute("""
                INSERT INTO domae_order_batches
                (id, "monitorId", status, "totalItems", "successCount", "failCount",
                 "createdAt", "completedAt")
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """, (
                batch_id, monitor_id, "completed", 1,
                1 if result.success else 0,
                0 if result.success else 1,
                utc_now, utc_now,
            ))
            cur.execute("""
                INSERT INTO domae_cloud_orders
                (id, "monitorId", "batchId", supplier, "productName", unit, "insuranceCode",
                 quantity, price, success, "productId", "orderId", message, "orderedAt")
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (
                _generate_cuid(), monitor_id, batch_id, supplier_name,
                product_name, unit, insurance_code,
                quantity, price, result.success,
                product_id, getattr(result, "order_id", None),
                getattr(result, "message", ""), utc_now,
            ))
            conn.commit()

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
                    crawler.login(cred.get("login_id", ""), cred.get("login_pw", ""))

                    scanned_at = datetime.now(timezone.utc).replace(tzinfo=None)
                    if first_scanned_at is None:
                        first_scanned_at = scanned_at
                    search_results = crawler.search(product_id_val)
                    available = 0
                    for sr in search_results:
                        if sr.product_id == product_id_val and sr.quantity and sr.quantity > 0:
                            available = sr.quantity
                            break

                    if available == 0:
                        supplier_results[supplier_name] = {"qty": 0}
                        continue

                    order_qty = min(remaining - filled_this_round, available)
                    result = crawler.order(product_id_val, order_qty)

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
