"""클라우드 모니터링 스케줄러"""
import importlib.util
import json
import logging
import os
import random
import string
import sys
import tempfile
import time
from datetime import datetime, timezone


def _generate_cuid() -> str:
    """Prisma cuid() 호환 ID 생성 (25자, 'c'로 시작)."""
    ts = int(time.time() * 1000)
    ts_part = ""
    base = 36
    while ts > 0:
        ts_part = string.digits[ts % base] if ts % base < 10 else chr(ord('a') + ts % base - 10) + ts_part
        ts //= base
    rand_part = ''.join(random.choices(string.ascii_lowercase + string.digits, k=16))
    return f"c{ts_part}{rand_part}"[:25]

logger = logging.getLogger(__name__)


class CloudScheduler:
    def __init__(self, db_pool, redis_client):
        self._db_pool = db_pool
        self._redis = redis_client
        self._crawlers = {}  # 캐시: {module_name: crawler_class}
        self._crawlers_loaded = False

    def execute(self, job: dict):
        """잡 1개 실행"""
        monitor_id = job["monitor_id"]
        conn = self._db_pool.getconn()
        try:
            # 1. 모니터 정보 조회
            cur = conn.cursor()
            cur.execute("""
                SELECT m.id, m.products, m.credentials,
                       m."telegramToken", m."telegramChatId", m."kakaoUserId",
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
            # 암호화된 문자열이면 복호화, 아니면 평문 JSON 호환
            if isinstance(raw_creds, str) and not raw_creds.startswith("{"):
                from domae_mcp.cloud.crypto import decrypt_credentials
                credentials = decrypt_credentials(raw_creds)
            else:
                credentials = json.loads(raw_creds) if isinstance(raw_creds, str) else raw_creds
            telegram_token = row[3]
            telegram_chat_id = row[4]
            tier = row[6]

            # 2. 크롤러 로드 (최초 1회)
            if not self._crawlers_loaded:
                self._load_crawlers(conn)

            # 3. 각 제품 검색
            all_results = []
            for keyword in products:
                results = self._search_all(keyword, credentials)
                all_results.extend(results)
                time.sleep(1)  # 제품 간 딜레이

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

            # 6. 변동 감지 + 알림
            all_changes = []
            for keyword in products:
                keyword_results = [r for r in all_results if r["keyword"] == keyword]
                if keyword_results:
                    changes = self._detect_changes(conn, monitor_id, keyword, keyword_results)
                    all_changes.extend(changes)

            if all_changes and (telegram_token or telegram_chat_id):
                from domae_mcp.cloud.notifier import Notifier
                message = f"🔔 도매 모니터링 알림\n\n" + "\n".join(all_changes)
                Notifier.send_telegram(telegram_token, telegram_chat_id, message)

            logger.info("모니터 %s 완료: %d건 검색, %d건 변동", monitor_id, len(all_results), len(all_changes))

        except Exception as e:
            conn.rollback()
            logger.error("모니터 실행 실패 [%s]: %s", monitor_id, e, exc_info=True)
        finally:
            self._db_pool.putconn(conn)

    def _load_crawlers(self, conn):
        """DB에서 크롤러 코드 로드 (동적 import)"""
        cur = conn.cursor()
        cur.execute('SELECT name, code FROM domae_crawlers WHERE "isActive" = true')
        rows = cur.fetchall()

        cache_dir = tempfile.mkdtemp(prefix="domae_cloud_")

        # base.py import 경로 확보
        # domae_mcp 패키지가 설치되어 있어야 함
        from domae_mcp.core.crawlers.base import BaseCrawler

        for name, code in rows:
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

                time.sleep(2)  # 도매사이트별 2초 딜레이

            except Exception as e:
                logger.warning("검색 실패 [%s/%s]: %s", supplier_name, keyword, e)

        return results

    def _save_results(self, conn, monitor_id: str, results: list):
        """검색 결과 DB 저장"""
        cur = conn.cursor()
        utc_now = datetime.now(timezone.utc)
        for r in results:
            cur.execute("""
                INSERT INTO domae_cloud_results
                (id, "monitorId", keyword, supplier, "productName", unit, "insuranceCode", price, quantity, "productId", "searchedAt")
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (
                _generate_cuid(), monitor_id, r["keyword"], r["supplier"], r["product_name"],
                r.get("unit"), r.get("insurance_code"), r.get("price"), r.get("quantity"), r.get("product_id"), utc_now,
            ))

    def _detect_changes(self, conn, monitor_id: str, keyword: str, new_results: list) -> list:
        """이전 검색 결과와 비교하여 변동 사항 감지.

        Returns:
            변동 메시지 리스트. 예: ["[지오영] 아모잘탄정 가격 하락: 12,500 → 11,800"]
        """
        cur = conn.cursor()
        # 직전 검색 결과 (현재 검색 직전의 searchedAt 기준)
        cur.execute("""
            SELECT DISTINCT ON (supplier, "productName")
                   supplier, "productName", price, quantity
            FROM domae_cloud_results
            WHERE "monitorId" = %s AND keyword = %s
            AND "searchedAt" < (SELECT MAX("searchedAt") FROM domae_cloud_results WHERE "monitorId" = %s AND keyword = %s)
            ORDER BY supplier, "productName", "searchedAt" DESC
        """, (monitor_id, keyword, monitor_id, keyword))

        prev_map = {}
        for row in cur.fetchall():
            key = f"{row[0]}|{row[1]}"  # supplier|productName
            prev_map[key] = {"price": row[2], "quantity": row[3]}

        if not prev_map:
            return []  # 첫 검색이면 비교 불가

        changes = []
        for r in new_results:
            key = f"{r['supplier']}|{r['product_name']}"
            prev = prev_map.get(key)

            if prev is None:
                # 새로 등장한 제품
                changes.append(f"🆕 [{r['supplier']}] {r['product_name']} 신규 등장 (가격: {r.get('price', '?')}원)")
                continue

            # 가격 하락
            if prev["price"] and r.get("price") and r["price"] < prev["price"]:
                changes.append(f"📉 [{r['supplier']}] {r['product_name']} 가격 하락: {prev['price']:,} → {r['price']:,}원")

            # 재고 증가 (0 → N)
            if (not prev["quantity"] or prev["quantity"] == 0) and r.get("quantity") and r["quantity"] > 0:
                changes.append(f"📦 [{r['supplier']}] {r['product_name']} 재고 입고: {r['quantity']}개")

    def search_on_demand(self, job: dict):
        """온디맨드 검색 — 도매상별로 stream_key에 결과를 실시간 전송"""
        monitor_id = job["monitor_id"]
        stream_key = job["stream_key"]
        keywords = job.get("keywords", [])
        requested_suppliers = job.get("suppliers", [])

        conn = self._db_pool.getconn()
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
            if isinstance(raw_creds, str) and not raw_creds.startswith("{"):
                from domae_mcp.cloud.crypto import decrypt_credentials
                credentials = decrypt_credentials(raw_creds)
            else:
                credentials = json.loads(raw_creds) if isinstance(raw_creds, str) else raw_creds

            # 2. 크롤러 로드
            if not self._crawlers_loaded:
                self._load_crawlers(conn)

            # 3. 대상 도매상 결정
            target_suppliers = {}
            for supplier_name, crawler_cls in self._crawlers.items():
                if requested_suppliers and supplier_name not in requested_suppliers:
                    continue
                cred = credentials.get(supplier_name)
                if not cred:
                    continue
                target_suppliers[supplier_name] = (crawler_cls, cred)

            # 4. 도매상별 검색 + 즉시 stream 전송
            for supplier_name, (crawler_cls, cred) in target_suppliers.items():
                try:
                    crawler = crawler_cls()
                    crawler.login(cred.get("login_id", ""), cred.get("login_pw", ""))

                    supplier_results = []
                    for keyword in keywords:
                        try:
                            search_results = crawler.search(keyword)
                            for r in search_results:
                                supplier_results.append({
                                    "keyword": keyword,
                                    "supplier": supplier_name,
                                    "product_name": r.product_name,
                                    "unit": r.unit,
                                    "insurance_code": getattr(r, "insurance_code", None),
                                    "quantity": r.quantity,
                                    "price": r.price,
                                    "product_id": r.product_id,
                                })
                        except Exception as e:
                            logger.warning("검색 실패 [%s/%s]: %s", supplier_name, keyword, e)
                        time.sleep(1)

                    # 도매상 1곳 완료 → stream 전송
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

                time.sleep(2)  # 도매상 간 딜레이

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

        conn = self._db_pool.getconn()
        try:
            # 1. credentials 조회
            cur = conn.cursor()
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
            if isinstance(raw_creds, str) and not raw_creds.startswith("{"):
                from domae_mcp.cloud.crypto import decrypt_credentials
                credentials = decrypt_credentials(raw_creds)
            else:
                credentials = json.loads(raw_creds) if isinstance(raw_creds, str) else raw_creds

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

        conn = self._db_pool.getconn()
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
                SELECT m.credentials, m."telegramToken", m."telegramChatId"
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
            if isinstance(raw_creds, str) and not raw_creds.startswith("{"):
                from domae_mcp.cloud.crypto import decrypt_credentials
                credentials = decrypt_credentials(raw_creds)
            else:
                credentials = json.loads(raw_creds) if isinstance(raw_creds, str) else raw_creds

            telegram_token = row[1]
            telegram_chat_id = row[2]

            # 3. 크롤러 로드
            if not self._crawlers_loaded:
                self._load_crawlers(conn)

            # 4. 순차 주문 처리
            success_count = 0
            fail_count = 0
            logged_in_crawlers = {}  # 도매상별 로그인 캐시

            for item in items:
                supplier_name = item.get("supplier")
                product_id_val = item.get("product_id")
                product_name = item.get("product_name", "")
                insurance_code = item.get("insurance_code")
                unit = item.get("unit")
                qty = item.get("quantity", 1)
                cart_item_id = item.get("cart_item_id")

                order_success = False
                order_id_val = None
                order_message = ""
                order_price = item.get("price")

                try:
                    if not supplier_name:
                        order_message = "도매상 미지정"
                        raise ValueError(order_message)

                    cred = credentials.get(supplier_name)
                    if not cred:
                        order_message = f"{supplier_name} 계정 미등록"
                        raise ValueError(order_message)

                    crawler_cls = self._crawlers.get(supplier_name)
                    if not crawler_cls:
                        order_message = f"{supplier_name} 크롤러 없음"
                        raise ValueError(order_message)

                    # 도매상별 로그인 캐시
                    if supplier_name not in logged_in_crawlers:
                        crawler = crawler_cls()
                        crawler.login(cred.get("login_id", ""), cred.get("login_pw", ""))
                        logged_in_crawlers[supplier_name] = crawler

                    crawler = logged_in_crawlers[supplier_name]
                    result = crawler.order(product_id_val, qty)
                    order_success = result.success
                    order_id_val = getattr(result, "order_id", None)
                    order_message = getattr(result, "message", "")

                except Exception as e:
                    order_message = order_message or str(e)

                # DomaeCloudOrder INSERT
                utc_now = datetime.now(timezone.utc)
                cur.execute("""
                    INSERT INTO domae_cloud_orders
                    (id, "monitorId", "batchId", supplier, "productName", unit, "insuranceCode",
                     quantity, price, success, "productId", "orderId", message, "orderedAt")
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """, (
                    _generate_cuid(), monitor_id, batch_id, supplier_name or "",
                    product_name, unit, insurance_code,
                    qty, order_price, order_success,
                    product_id_val, order_id_val, order_message, utc_now,
                ))

                if order_success:
                    success_count += 1
                    # 장바구니에서 제거
                    if cart_item_id:
                        cur.execute('DELETE FROM domae_cart_items WHERE id = %s', (cart_item_id,))
                else:
                    fail_count += 1
                    # 장바구니에 실패 사유 기록
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

                time.sleep(2)  # 주문 간 딜레이

            # 5. batch 완료
            utc_now = datetime.now(timezone.utc)
            cur.execute("""
                UPDATE domae_order_batches
                SET status = %s, "completedAt" = %s
                WHERE id = %s
            """, ("completed", utc_now, batch_id))
            conn.commit()

            # 6. 텔레그램 알림
            if telegram_token or telegram_chat_id:
                try:
                    from domae_mcp.cloud.notifier import Notifier
                    msg = f"📦 도매 일괄주문 완료\n\n성공: {success_count}건\n실패: {fail_count}건"
                    Notifier.send_telegram(telegram_token, telegram_chat_id, msg)
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
        """긴급주문 즉시 실행 (F-4에서 구현)"""
        logger.info("urgent_order_immediate: %s (미구현)", job.get("monitor_id"))
