"""Redis 잡 소비자"""
import json
import logging
import os
import signal
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import redis
import psycopg2
from psycopg2 import pool

from domae_mcp.cloud.scheduler import CloudScheduler

logger = logging.getLogger(__name__)


class CloudWorker:
    def __init__(self):
        self._running = True
        self._redis = redis.Redis.from_url(
            os.environ.get("REDIS_URL", "redis://localhost:6379"),
            decode_responses=True,
        )
        self._db_pool = pool.SimpleConnectionPool(
            1, 3,  # min 1, max 3 connections
            dsn=os.environ["DATABASE_URL"],
        )
        self._executor = ThreadPoolExecutor(max_workers=3)
        self._scheduler = CloudScheduler(self._db_pool, self._redis)

        signal.signal(signal.SIGINT, self._shutdown)
        signal.signal(signal.SIGTERM, self._shutdown)

    def _shutdown(self, signum, frame):
        logger.info("워커 종료 시작...")
        self._running = False

    def run(self):
        logger.info("도매 클라우드 워커 시작 (BRPOP 대기)")

        while self._running:
            try:
                result = self._redis.brpop("domae:jobs", timeout=5)
                if result is None:
                    continue

                _, job_data = result
                job = json.loads(job_data)
                monitor_id = job.get("monitor_id")

                logger.info("잡 수신: %s", monitor_id)

                try:
                    self._scheduler.execute(job)
                except Exception as e:
                    logger.error("잡 실행 실패 [%s]: %s", monitor_id, e)
                finally:
                    # 실행 완료 → 락 해제
                    self._redis.delete(f"domae:running:{monitor_id}")

            except redis.ConnectionError:
                logger.warning("Redis 연결 끊김, 5초 후 재시도")
                time.sleep(5)
            except Exception as e:
                logger.error("워커 에러: %s", e, exc_info=True)

        logger.info("워커 종료 완료")
        self._executor.shutdown(wait=True)
        self._db_pool.closeall()
