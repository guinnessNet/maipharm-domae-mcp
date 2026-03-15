"""모니터링 스케줄 모델"""

from sqlalchemy import Integer
from sqlalchemy.orm import Mapped, mapped_column

from domae_mcp.core.models.base import Base


class MonitorSchedule(Base):
    __tablename__ = "monitor_schedules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    start_hour: Mapped[int] = mapped_column(Integer, nullable=False)
    end_hour: Mapped[int] = mapped_column(Integer, nullable=False)
    interval_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
