"""Redis 잡 소비자 — 우선순위 큐 지원"""
import json
import logging
import os
import signal
import sys
import time
from concurrent.futures import ThreadPoolExecutor, Future

import redis
import psycopg2
from psycopg2 import pool

from domae_mcp.cloud.scheduler import CloudScheduler

logger = logging.getLogger(__name__)

# 사용자 요청 (검색/주문/검증) → 우선 큐
PRIORITY_QUEUE = "domae:jobs:urgent"
# 모니터링 잡 → 일반 큐
NORMAL_QUEUE = "domae:jobs"

# 우선 처리 대상 action
PRIORITY_ACTIONS = {"search_on_demand", "order", "batch_order", "urgent_order_immediate", "verify_credentials"}


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
        self._monitor_future: Future | None = None

        signal.signal(signal.SIGINT, self._shutdown)
        signal.signal(signal.SIGTERM, self._shutdown)

    def _shutdown(self, signum, frame):
        logger.info("워커 종료 시작...")
        self._running = False

    def _process_job(self, job: dict):
        """잡 실행 (스레드에서 호출 가능)"""
        monitor_id = job.get("monitor_id")
        action = job.get("action", "monitor")

        try:
            if action in ("monitor", "search"):
                self._scheduler.execute(job)
            elif action == "search_on_demand":
                self._scheduler.search_on_demand(job)
            elif action == "order":
                self._scheduler.order(job)
            elif action == "batch_order":
                self._scheduler.batch_order(job)
            elif action == "urgent_order_immediate":
                self._scheduler.urgent_order_immediate(job)
            elif action == "verify_credentials":
                self._scheduler.verify_credentials(job)
            else:
                logger.warning("알 수 없는 action: %s", action)
        except Exception as e:
            logger.error("잡 실행 실패 [%s/%s]: %s", monitor_id, action, e)
        finally:
            if action in ("monitor", "search") and monitor_id:
                self._redis.delete(f"domae:running:{monitor_id}")

    def run(self):
        logger.info("도매 클라우드 워커 시작 (우선순위 큐 지원)")

        while self._running:
            try:
                # 1. 우선 큐 먼저 확인 (사용자 검색/주문)
                urgent = self._redis.lpop(PRIORITY_QUEUE)
                if urgent:
                    job = json.loads(urgent)
                    logger.info("우선 잡 수신: monitor=%s action=%s", job.get("monitor_id"), job.get("action"))
                    self._process_job(job)
                    continue

                # 2. 일반 큐 (모니터링) — 5초 대기
                result = self._redis.brpop(NORMAL_QUEUE, timeout=5)
                if result is None:
                    continue

                _, job_data = result
                job = json.loads(job_data)
                action = job.get("action", "monitor")

                # 일반 큐에 우선 잡이 섞여 들어온 경우 (하위 호환)
                if action in PRIORITY_ACTIONS:
                    logger.info("우선 잡 수신(일반큐): monitor=%s action=%s", job.get("monitor_id"), action)
                    self._process_job(job)
                    continue

                logger.info("잡 수신: monitor=%s action=%s", job.get("monitor_id"), action)
                self._process_job(job)

            except redis.ConnectionError:
                logger.warning("Redis 연결 끊김, 5초 후 재시도")
                time.sleep(5)
            except Exception as e:
                logger.error("워커 에러: %s", e, exc_info=True)

        logger.info("워커 종료 완료")
        self._executor.shutdown(wait=True)
        self._db_pool.closeall()
