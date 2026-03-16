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
from datetime import datetime


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
            cur.execute(
                'UPDATE domae_cloud_monitors SET "lastRunAt" = NOW(), "updatedAt" = NOW() WHERE id = %s',
                (monitor_id,)
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
        for r in results:
            cur.execute("""
                INSERT INTO domae_cloud_results
                (id, "monitorId", keyword, supplier, "productName", unit, price, quantity, "productId", "searchedAt")
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
            """, (
                _generate_cuid(), monitor_id, r["keyword"], r["supplier"], r["product_name"],
                r.get("unit"), r.get("price"), r.get("quantity"), r.get("product_id"),
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
