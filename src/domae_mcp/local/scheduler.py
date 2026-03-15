"""로컬 스케줄러: MonitorService를 래핑하여 최소 60분 간격 제한 적용"""

import logging
import threading
import time
from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from domae_mcp.core.models import MonitorSchedule
from domae_mcp.core.services.monitor_service import MonitorService

logger = logging.getLogger(__name__)

# 로컬 모드 최소 모니터링 간격 (분)
LOCAL_MIN_INTERVAL = 60


class LocalScheduler:
    """로컬 모드 스케줄러. 최소 간격 60분.

    MonitorService를 래핑하며, MonitorSchedule 테이블의 시간대별 간격을
    LOCAL_MIN_INTERVAL 이상으로 보정하여 적용한다.
    백그라운드 스레드로 실행.
    """

    def __init__(
        self,
        monitor_service: MonitorService,
        db_session_factory,
    ):
        """
        Args:
            monitor_service: 코어 모니터링 서비스 인스턴스.
            db_session_factory: SQLAlchemy Session 팩토리 (callable -> Session).
        """
        self._monitor = monitor_service
        self._db_session_factory = db_session_factory
        self._interval: int = LOCAL_MIN_INTERVAL  # 분 단위
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._is_running = False
        self._last_run: Optional[datetime] = None

    @property
    def is_running(self) -> bool:
        return self._is_running

    @property
    def last_run(self) -> Optional[datetime]:
        return self._last_run

    @property
    def interval(self) -> int:
        """현재 설정된 간격 (분)."""
        return self._interval

    def set_interval(self, interval_minutes: int) -> None:
        """모니터링 간격 설정. LOCAL_MIN_INTERVAL 미만이면 거부.

        Args:
            interval_minutes: 설정할 간격 (분).

        Raises:
            ValueError: 간격이 60분 미만인 경우.
        """
        if interval_minutes < LOCAL_MIN_INTERVAL:
            raise ValueError(
                f"로컬 모드 최소 모니터링 간격은 {LOCAL_MIN_INTERVAL}분입니다. "
                f"더 짧은 간격은 팜스퀘어 클라우드 서비스를 이용해주세요."
            )
        self._interval = interval_minutes
        logger.info("모니터링 간격 변경: %d분", interval_minutes)

    def start(self) -> None:
        """스케줄러 시작. 백그라운드 스레드로 실행."""
        if self._is_running:
            logger.info("스케줄러 이미 실행 중")
            return

        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="local-scheduler")
        self._thread.start()
        self._is_running = True
        logger.info("로컬 스케줄러 시작 (간격: %d분)", self._interval)

    def stop(self) -> None:
        """스케줄러 중지."""
        if not self._is_running:
            return

        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=10)
        self._is_running = False
        logger.info("로컬 스케줄러 중지")

    def _loop(self) -> None:
        """메인 루프: 시간대별 간격에 따라 MonitorService.run_cycle() 호출."""
        while not self._stop_event.is_set():
            try:
                self._monitor.run_cycle()
                self._last_run = datetime.now()
            except Exception:
                logger.error("스케줄러 사이클 에러", exc_info=True)

            # 현재 시간대에 맞는 간격 조회 (최소 60분 보정)
            interval_seconds = self._get_current_interval_seconds()

            # 1초 단위로 체크하며 대기 (stop 요청 즉시 반응)
            waited = 0
            while waited < interval_seconds and not self._stop_event.is_set():
                time.sleep(1)
                waited += 1

    def _get_current_interval_seconds(self) -> int:
        """현재 시간대에 맞는 모니터링 간격(초) 반환.

        MonitorSchedule 테이블에서 현재 시간에 해당하는 간격을 조회하고,
        LOCAL_MIN_INTERVAL 미만이면 LOCAL_MIN_INTERVAL로 보정한다.
        """
        db: Session = self._db_session_factory()
        try:
            now_hour = datetime.now().hour
            schedule = (
                db.query(MonitorSchedule)
                .filter(
                    MonitorSchedule.start_hour <= now_hour,
                    MonitorSchedule.end_hour > now_hour,
                )
                .first()
            )

            if schedule:
                interval_minutes = max(schedule.interval_minutes, LOCAL_MIN_INTERVAL)
            else:
                interval_minutes = self._interval

            return interval_minutes * 60

        except Exception:
            logger.warning("스케줄 조회 에러, 기본 간격 사용", exc_info=True)
            return self._interval * 60
        finally:
            db.close()
