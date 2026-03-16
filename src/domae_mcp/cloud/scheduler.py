"""클라우드 모니터링 스케줄러"""
import importlib.util
import json
import logging
import os
import sys
import tempfile
import time
from datetime import datetime

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
            credentials = json.loads(row[2]) if isinstance(row[2], str) else row[2]
            telegram_token = row[3]
            telegram_chat_id = row[4]
            tier = row[6]

            # TODO: credentials AES-256-GCM 복호화 (Phase 2)
            # credential_key = os.environ.get("DOMAE_CREDENTIAL_KEY")

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

            # 6. 알림 (변동 감지 시)
            # TODO: 이전 결과 비교 + 알림 발송

            logger.info("모니터 %s 완료: %d건 검색", monitor_id, len(all_results))

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
                VALUES (gen_random_uuid(), %s, %s, %s, %s, %s, %s, %s, %s, NOW())
            """, (
                monitor_id, r["keyword"], r["supplier"], r["product_name"],
                r.get("unit"), r.get("price"), r.get("quantity"), r.get("product_id"),
            ))
