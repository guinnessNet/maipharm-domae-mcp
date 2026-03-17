"""주문 이력 모델"""

from datetime import datetime

from sqlalchemy import Integer, Text, Boolean, DateTime
from sqlalchemy.orm import Mapped, mapped_column

from domae_mcp.core.models.base import Base


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    supplier: Mapped[str] = mapped_column(Text, nullable=False)
    product_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    product_name: Mapped[str] = mapped_column(Text, nullable=False)
    unit: Mapped[str | None] = mapped_column(Text, nullable=True)
    quantity: Mapped[int | None] = mapped_column(Integer, nullable=True)
    price: Mapped[int | None] = mapped_column(Integer, nullable=True)
    success: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_urgent: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    ordered_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
