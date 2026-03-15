"""긴급주문 관련 모델"""

from datetime import datetime
from typing import List

from sqlalchemy import Integer, Text, Boolean, DateTime, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column, relationship

from domae_mcp.core.models.base import Base


class UrgentOrder(Base):
    __tablename__ = "urgent_orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    product_name: Mapped[str] = mapped_column(Text, nullable=False)
    unit: Mapped[str | None] = mapped_column(Text, nullable=True)
    insurance_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    total_quantity: Mapped[int | None] = mapped_column(Integer, nullable=True)
    filled_quantity: Mapped[int] = mapped_column(Integer, default=0)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    suppliers: Mapped[List["UrgentOrderSupplier"]] = relationship(
        back_populates="urgent_order", cascade="all, delete-orphan"
    )
    logs: Mapped[List["UrgentOrderLog"]] = relationship(
        back_populates="urgent_order", cascade="all, delete-orphan"
    )


class UrgentOrderSupplier(Base):
    __tablename__ = "urgent_order_suppliers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    urgent_order_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("urgent_orders.id"), nullable=False
    )
    supplier: Mapped[str] = mapped_column(Text, nullable=False)
    product_id: Mapped[str] = mapped_column(Text, nullable=False)
    price: Mapped[int | None] = mapped_column(Integer, nullable=True)

    urgent_order: Mapped["UrgentOrder"] = relationship(back_populates="suppliers")


class UrgentOrderLog(Base):
    __tablename__ = "urgent_order_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    urgent_order_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("urgent_orders.id"), nullable=False
    )
    supplier: Mapped[str | None] = mapped_column(Text, nullable=True)
    ordered_quantity: Mapped[int | None] = mapped_column(Integer, nullable=True)
    success: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    ordered_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    urgent_order: Mapped["UrgentOrder"] = relationship(back_populates="logs")
