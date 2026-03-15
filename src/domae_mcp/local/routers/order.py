"""주문 라우터: 주문 실행 및 이력 조회"""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from domae_mcp.core.models import Order
from domae_mcp.core.services.order_service import OrderService
from domae_mcp.local.config import ConfigManager
from domae_mcp.local.database import get_db

router = APIRouter(tags=["주문"])

_order_service = OrderService()
_config = ConfigManager()


# ── Request/Response 스키마 ──


class PlaceOrderRequest(BaseModel):
    supplier: str
    product_id: str
    product_name: str
    quantity: int


class PlaceOrderResponse(BaseModel):
    success: bool
    message: str
    order_id: Optional[str] = None


class OrderHistoryItem(BaseModel):
    id: int
    supplier: str
    product_name: str
    unit: Optional[str] = None
    quantity: Optional[int] = None
    price: Optional[int] = None
    success: Optional[bool] = None
    message: Optional[str] = None
    is_urgent: Optional[bool] = None
    ordered_at: str


class OrderHistoryResponse(BaseModel):
    orders: list[OrderHistoryItem]
    total: int


# ── 엔드포인트 ──


@router.post("/orders", response_model=PlaceOrderResponse)
def place_order(req: PlaceOrderRequest, db: Session = Depends(get_db)):
    """도매상에 주문 실행."""
    cred = _config.get_credentials(req.supplier)
    if not cred.get("login_id") or not cred.get("login_pw"):
        raise HTTPException(
            status_code=400,
            detail=f"{req.supplier} 계정이 설정되지 않았습니다.",
        )

    result = _order_service.place_order(
        supplier=req.supplier,
        product_id=req.product_id,
        product_name=req.product_name,
        quantity=req.quantity,
        credentials=cred,
        db_session=db,
    )

    order_id = None
    if result.success:
        # 방금 저장된 마지막 주문 ID 조회
        last_order = db.query(Order).order_by(Order.id.desc()).first()
        if last_order:
            order_id = f"ORD-{last_order.id}"

    return PlaceOrderResponse(
        success=result.success,
        message=result.message,
        order_id=order_id,
    )


@router.get("/orders", response_model=OrderHistoryResponse)
def get_orders(
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    """주문 이력 조회."""
    total = db.query(Order).count()
    orders = (
        db.query(Order)
        .order_by(Order.ordered_at.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )

    items = [
        OrderHistoryItem(
            id=o.id,
            supplier=o.supplier,
            product_name=o.product_name,
            unit=o.unit,
            quantity=o.quantity,
            price=o.price,
            success=o.success,
            message=o.message,
            is_urgent=o.is_urgent,
            ordered_at=o.ordered_at.isoformat() if o.ordered_at else "",
        )
        for o in orders
    ]

    return OrderHistoryResponse(orders=items, total=total)
