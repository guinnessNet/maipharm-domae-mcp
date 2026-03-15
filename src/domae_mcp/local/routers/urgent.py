"""긴급주문 라우터: CRUD + 취소/재활성화"""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from domae_mcp.core.models import UrgentOrder, UrgentOrderSupplier
from domae_mcp.local.database import get_db

router = APIRouter(tags=["긴급주문"])


# ── Request/Response 스키마 ──


class UrgentSupplierInput(BaseModel):
    supplier: str
    product_id: str
    price: Optional[int] = None


class CreateUrgentOrderRequest(BaseModel):
    product_name: str
    unit: Optional[str] = None
    insurance_code: Optional[str] = None
    total_quantity: int
    suppliers: list[UrgentSupplierInput]


class UrgentSupplierOut(BaseModel):
    supplier: str
    product_id: str
    price: Optional[int] = None


class UrgentLogOut(BaseModel):
    supplier: Optional[str] = None
    ordered_quantity: Optional[int] = None
    success: Optional[bool] = None
    created_at: Optional[str] = None


class UrgentOrderOut(BaseModel):
    id: int
    product_name: str
    unit: Optional[str] = None
    total_quantity: Optional[int] = None
    filled_quantity: int
    active: bool
    created_at: str
    suppliers: list[UrgentSupplierOut]
    logs: list[UrgentLogOut]


class UrgentOrderListResponse(BaseModel):
    orders: list[UrgentOrderOut]


class MessageResponse(BaseModel):
    success: bool
    message: str


# ── 헬퍼 ──


def _serialize_urgent_order(uo: UrgentOrder) -> UrgentOrderOut:
    return UrgentOrderOut(
        id=uo.id,
        product_name=uo.product_name,
        unit=uo.unit,
        total_quantity=uo.total_quantity,
        filled_quantity=uo.filled_quantity or 0,
        active=uo.active,
        created_at=uo.created_at.isoformat() if uo.created_at else "",
        suppliers=[
            UrgentSupplierOut(
                supplier=s.supplier,
                product_id=s.product_id,
                price=s.price,
            )
            for s in uo.suppliers
        ],
        logs=[
            UrgentLogOut(
                supplier=log.supplier,
                ordered_quantity=log.ordered_quantity,
                success=log.success,
                created_at=log.ordered_at.isoformat() if log.ordered_at else None,
            )
            for log in uo.logs
        ],
    )


# ── 엔드포인트 ──


@router.get("/urgent-orders", response_model=UrgentOrderListResponse)
def list_urgent_orders(db: Session = Depends(get_db)):
    """긴급주문 목록 조회."""
    orders = db.query(UrgentOrder).order_by(UrgentOrder.created_at.desc()).all()
    return UrgentOrderListResponse(
        orders=[_serialize_urgent_order(uo) for uo in orders]
    )


@router.post("/urgent-orders", response_model=UrgentOrderOut, status_code=201)
def create_urgent_order(req: CreateUrgentOrderRequest, db: Session = Depends(get_db)):
    """긴급주문 등록."""
    uo = UrgentOrder(
        product_name=req.product_name,
        unit=req.unit,
        insurance_code=req.insurance_code,
        total_quantity=req.total_quantity,
        filled_quantity=0,
        active=True,
    )
    for s in req.suppliers:
        uo.suppliers.append(
            UrgentOrderSupplier(
                supplier=s.supplier,
                product_id=s.product_id,
                price=s.price,
            )
        )
    db.add(uo)
    db.commit()
    db.refresh(uo)
    return _serialize_urgent_order(uo)


@router.put("/urgent-orders/{order_id}/cancel", response_model=MessageResponse)
def cancel_urgent_order(order_id: int, db: Session = Depends(get_db)):
    """긴급주문 취소."""
    uo = db.query(UrgentOrder).filter(UrgentOrder.id == order_id).first()
    if not uo:
        raise HTTPException(status_code=404, detail="긴급주문을 찾을 수 없습니다.")
    if not uo.active:
        raise HTTPException(status_code=400, detail="이미 비활성 상태입니다.")

    uo.active = False
    db.commit()
    return MessageResponse(success=True, message="긴급주문이 취소되었습니다.")


@router.put("/urgent-orders/{order_id}/reactivate", response_model=MessageResponse)
def reactivate_urgent_order(order_id: int, db: Session = Depends(get_db)):
    """완료/취소된 긴급주문 재활성화."""
    uo = db.query(UrgentOrder).filter(UrgentOrder.id == order_id).first()
    if not uo:
        raise HTTPException(status_code=404, detail="긴급주문을 찾을 수 없습니다.")
    if uo.active:
        raise HTTPException(status_code=400, detail="이미 활성 상태입니다.")

    uo.active = True
    uo.completed_at = None
    db.commit()
    return MessageResponse(success=True, message="긴급주문이 재활성화되었습니다.")


@router.delete("/urgent-orders/{order_id}", response_model=MessageResponse)
def delete_urgent_order(order_id: int, db: Session = Depends(get_db)):
    """긴급주문 삭제."""
    uo = db.query(UrgentOrder).filter(UrgentOrder.id == order_id).first()
    if not uo:
        raise HTTPException(status_code=404, detail="긴급주문을 찾을 수 없습니다.")

    db.delete(uo)
    db.commit()
    return MessageResponse(success=True, message="긴급주문이 삭제되었습니다.")
