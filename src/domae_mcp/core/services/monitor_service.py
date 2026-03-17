"""모니터링 서비스: 백그라운드 스레드로 재고 변동 감시"""

import logging
import threading
import time
from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from domae_mcp.core.models import (
    Product,
    InventorySnapshot,
    MonitorSchedule,
    MonitorAlert,
    UrgentOrder,
    UrgentOrderLog,
)
from domae_mcp.core.services.search_service import SearchService
from domae_mcp.core.services.order_service import OrderService
from domae_mcp.core.services.telegram_service import TelegramService

logger = logging.getLogger(__name__)

# 스케줄 미등록 시 기본 간격 (분)
DEFAULT_INTERVAL_MINUTES = 30


class MonitorService:
    """백그라운드 스레드로 모니터링 대상 제품의 재고를 주기적으로 검색.

    변동 감지 시 텔레그램 알림, 긴급주문 조건 충족 시 자동 주문.
    """

    def __init__(
        self,
        db_session_factory,
        credentials: dict[str, dict[str, str]],
        telegram_service: Optional[TelegramService] = None,
    ):
        """
        Args:
            db_session_factory: SQLAlchemy Session 팩토리 (callable → Session).
            credentials: {도매상명: {"login_id": ..., "login_pw": ...}}.
            telegram_service: TelegramService 인스턴스 (None이면 알림 비활성).
        """
        self._db_session_factory = db_session_factory
        self._credentials = credentials
        self._telegram = telegram_service or TelegramService()
        self._search_service = SearchService()
        self._order_service = OrderService()

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

    def start(self):
        """모니터링 시작."""
        if self._is_running:
            logger.info("모니터링 이미 실행 중")
            return

        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        self._is_running = True
        logger.info("모니터링 시작")

    def stop(self):
        """모니터링 중지."""
        if not self._is_running:
            return

        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=10)
        self._is_running = False
        logger.info("모니터링 중지")

    def update_credentials(self, credentials: dict[str, dict[str, str]]):
        """런타임에 계정 정보 갱신."""
        self._credentials = credentials

    def update_telegram(self, telegram_service: TelegramService):
        """런타임에 텔레그램 서비스 갱신."""
        self._telegram = telegram_service

    def _loop(self):
        """메인 루프: 스케줄에 따라 주기적으로 run_cycle 실행."""
        while not self._stop_event.is_set():
            try:
                self.run_cycle()
            except Exception:
                logger.error("모니터링 사이클 에러", exc_info=True)

            interval = self._get_current_interval()
            # 1초 단위로 체크하며 대기 (stop 요청 즉시 반응)
            waited = 0
            while waited < interval and not self._stop_event.is_set():
                time.sleep(1)
                waited += 1

    def run_cycle(self):
        """모니터링 1회 사이클 실행."""
        db: Session = self._db_session_factory()
        try:
            # 1. 모니터링 대상 제품 조회
            products = db.query(Product).all()
            if not products:
                logger.debug("모니터링 대상 제품 없음")
                return

            for product in products:
                try:
                    self._process_product(db, product)
                except Exception:
                    logger.warning(
                        "제품 처리 에러: %s", product.name, exc_info=True
                    )

            self._last_run = datetime.now()
        finally:
            db.close()

    def _process_product(self, db: Session, product: Product):
        """개별 제품 처리: 검색 → 비교 → 알림 → 긴급주문 → 스냅샷 저장."""
        # 2. 검색
        grouped_results = self._search_service.search(
            keyword=product.name,
            credentials=self._credentials,
        )

        # 현재 결과를 flat하게 변환 (supplier 단위)
        current_items: list[dict] = []
        for group in grouped_results:
            for sup in group["suppliers"]:
                current_items.append({
                    "maker": group["maker"],
                    "product_name": group["product_name"],
                    "unit": group["unit"],
                    "insurance_code": group["insurance_code"],
                    "supplier": sup["name"],
                    "quantity": sup["quantity"],
                    "price": sup["price"],
                    "product_id": sup["product_id"],
                })

        # 3. 이전 스냅샷 조회 (해당 제품 키워드 기반)
        prev_snapshots = (
            db.query(InventorySnapshot)
            .filter(InventorySnapshot.product_name.isnot(None))
            .all()
        )
        prev_map = {
            (s.product_name, s.supplier): s for s in prev_snapshots
        }

        # 4. 변동 감지 및 알림
        for item in current_items:
            key = (item["product_name"], item["supplier"])
            prev = prev_map.get(key)

            if prev is None:
                # 신규 재고 감지
                if item["quantity"] > 0:
                    self._telegram.send_stock_alert(
                        product_name=item["product_name"],
                        supplier=item["supplier"],
                        quantity=item["quantity"],
                        price=item["price"],
                    )
                    db.add(MonitorAlert(
                        product_name=item["product_name"],
                        supplier=item["supplier"],
                        alert_type="stock",
                        old_value=0,
                        new_value=float(item["quantity"]),
                    ))
            else:
                # 가격 변동
                if prev.price and item["price"] and prev.price != item["price"]:
                    self._telegram.send_price_alert(
                        product_name=item["product_name"],
                        supplier=item["supplier"],
                        old_price=prev.price,
                        new_price=item["price"],
                    )
                    db.add(MonitorAlert(
                        product_name=item["product_name"],
                        supplier=item["supplier"],
                        alert_type="price",
                        old_value=float(prev.price),
                        new_value=float(item["price"]),
                    ))
                # 재고 없음 → 있음
                if (prev.quantity or 0) == 0 and item["quantity"] > 0:
                    self._telegram.send_stock_alert(
                        product_name=item["product_name"],
                        supplier=item["supplier"],
                        quantity=item["quantity"],
                        price=item["price"],
                    )
                    db.add(MonitorAlert(
                        product_name=item["product_name"],
                        supplier=item["supplier"],
                        alert_type="stock",
                        old_value=0,
                        new_value=float(item["quantity"]),
                    ))

        # 5. 긴급주문 체크
        self._check_urgent_orders(db, current_items)

        # 6. 새 스냅샷 저장 (기존 스냅샷 삭제 후 교체)
        for s in prev_snapshots:
            db.delete(s)

        for item in current_items:
            snapshot = InventorySnapshot(
                maker=item["maker"],
                product_name=item["product_name"],
                unit=item["unit"],
                insurance_code=item["insurance_code"],
                quantity=item["quantity"],
                supplier=item["supplier"],
                price=item["price"],
                product_id=item["product_id"],
                scanned_at=datetime.now(),
            )
            db.add(snapshot)

        db.commit()

    def _check_urgent_orders(self, db: Session, current_items: list[dict]):
        """긴급주문 조건 충족 시 자동 주문 실행."""
        urgent_orders = (
            db.query(UrgentOrder)
            .filter(UrgentOrder.active.is_(True))
            .all()
        )

        for uo in urgent_orders:
            remaining = (uo.total_quantity or 0) - (uo.filled_quantity or 0)
            if remaining <= 0:
                continue

            # 이 긴급주문에 등록된 도매상 목록
            for uo_sup in uo.suppliers:
                if remaining <= 0:
                    break

                # 현재 검색 결과에서 해당 도매/제품 재고 확인
                match = next(
                    (
                        item
                        for item in current_items
                        if item["supplier"] == uo_sup.supplier
                        and item["product_id"] == uo_sup.product_id
                        and item["quantity"] > 0
                    ),
                    None,
                )
                if match is None:
                    continue

                order_qty = min(remaining, match["quantity"])

                cred = self._credentials.get(uo_sup.supplier)
                if not cred:
                    continue

                result = self._order_service.place_order(
                    supplier=uo_sup.supplier,
                    product_id=uo_sup.product_id,
                    product_name=uo.product_name,
                    quantity=order_qty,
                    credentials=cred,
                    db_session=db,
                    is_urgent=True,
                )

                # 로그 저장
                log = UrgentOrderLog(
                    urgent_order_id=uo.id,
                    supplier=uo_sup.supplier,
                    ordered_quantity=order_qty,
                    success=result.success,
                    message=result.message,
                    ordered_at=datetime.now(),
                )
                db.add(log)

                # 텔레그램 알림
                self._telegram.send_order_alert(
                    product_name=uo.product_name,
                    supplier=uo_sup.supplier,
                    quantity=order_qty,
                    success=result.success,
                    message=result.message,
                )

                if result.success:
                    uo.filled_quantity = (uo.filled_quantity or 0) + order_qty
                    remaining -= order_qty

            # 수량 충족 시 비활성화
            if uo.filled_quantity >= (uo.total_quantity or 0):
                uo.active = False
                uo.completed_at = datetime.now()

        db.commit()

    def _get_current_interval(self) -> int:
        """현재 시간대에 맞는 모니터링 간격(초) 반환."""
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
                return schedule.interval_minutes * 60
            return DEFAULT_INTERVAL_MINUTES * 60
        except Exception:
            logger.warning("스케줄 조회 에러", exc_info=True)
            return DEFAULT_INTERVAL_MINUTES * 60
        finally:
            db.close()
