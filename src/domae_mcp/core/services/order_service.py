"""주문 서비스"""

import logging
from datetime import datetime

from sqlalchemy.orm import Session

from domae_mcp.core.crawlers import CrawlerRegistry, OrderResult
from domae_mcp.core.models import Order

logger = logging.getLogger(__name__)


class OrderService:
    """도매상 주문 실행 및 DB 기록."""

    def place_order(
        self,
        supplier: str,
        product_id: str,
        product_name: str,
        quantity: int,
        credentials: dict[str, str],
        db_session: Session,
        is_urgent: bool = False,
    ) -> OrderResult:
        """주문 실행 후 결과를 DB에 저장.

        Args:
            supplier: 도매상명.
            product_id: 도매상 내부 제품코드.
            product_name: 제품명.
            quantity: 주문 수량.
            credentials: {"login_id": ..., "login_pw": ...}.
            db_session: SQLAlchemy 세션.
            is_urgent: 긴급주문 여부.

        Returns:
            OrderResult.
        """
        if not CrawlerRegistry.is_loaded():
            result = OrderResult(
                success=False,
                message="크롤러가 로드되지 않았습니다. API 키를 확인하세요.",
            )
        else:
            try:
                crawler = CrawlerRegistry.get(supplier)
                crawler.ensure_login(credentials["login_id"], credentials["login_pw"])
                # 토큰/단가 캐시가 필요한 크롤러(TJ팜 등)를 위해 product_name 으로 선행 search.
                # 결과는 버리고 크롤러 내부 상태만 확보.
                if product_name:
                    try:
                        crawler.search(product_name)
                    except Exception:
                        logger.warning("order 전 선행 search 실패: %s / %s", supplier, product_name)
                result = crawler.order(product_id, quantity)
            except Exception as e:
                logger.error("주문 실패: %s %s", supplier, product_name, exc_info=True)
                result = OrderResult(success=False, message=str(e))

        # DB 저장
        order = Order(
            supplier=supplier,
            product_id=product_id,
            product_name=product_name,
            quantity=quantity,
            success=result.success,
            message=result.message,
            is_urgent=is_urgent,
            ordered_at=datetime.now(),
        )
        db_session.add(order)
        db_session.commit()

        return result
