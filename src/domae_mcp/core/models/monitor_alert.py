"""모니터링 변동 알림 이력 모델"""

from datetime import datetime, timezone

from sqlalchemy import Integer, Text, Float, DateTime
from sqlalchemy.orm import Mapped, mapped_column

from domae_mcp.core.models.base import Base


class MonitorAlert(Base):
    __tablename__ = "monitor_alerts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    product_name: Mapped[str] = mapped_column(Text, nullable=False)
    supplier: Mapped[str] = mapped_column(Text, nullable=False)
    alert_type: Mapped[str] = mapped_column(Text, nullable=False)  # "price" or "stock"
    old_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    new_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
