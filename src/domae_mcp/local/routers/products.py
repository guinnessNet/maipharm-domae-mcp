"""모니터링 제품 라우터: 제품 CRUD"""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from domae_mcp.core.models import Product
from domae_mcp.local.database import get_db

router = APIRouter(tags=["모니터링 제품"])


# ── Request/Response 스키마 ──


class AddProductRequest(BaseModel):
    name: str
    description: Optional[str] = None


class ProductOut(BaseModel):
    id: int
    name: str
    description: Optional[str] = None
    created_at: str


class ProductListResponse(BaseModel):
    products: list[ProductOut]


class MessageResponse(BaseModel):
    success: bool
    message: str


# ── 엔드포인트 ──


@router.get("/products", response_model=ProductListResponse)
def list_products(db: Session = Depends(get_db)):
    """모니터링 대상 제품 목록 조회."""
    products = db.query(Product).order_by(Product.created_at.desc()).all()
    return ProductListResponse(
        products=[
            ProductOut(
                id=p.id,
                name=p.name,
                description=p.description,
                created_at=p.created_at.isoformat() if p.created_at else "",
            )
            for p in products
        ]
    )


@router.post("/products", response_model=ProductOut, status_code=201)
def add_product(req: AddProductRequest, db: Session = Depends(get_db)):
    """모니터링 대상 제품 추가."""
    product = Product(name=req.name, description=req.description)
    db.add(product)
    db.commit()
    db.refresh(product)
    return ProductOut(
        id=product.id,
        name=product.name,
        description=product.description,
        created_at=product.created_at.isoformat() if product.created_at else "",
    )


@router.delete("/products/{product_id}", response_model=MessageResponse)
def delete_product(product_id: int, db: Session = Depends(get_db)):
    """모니터링 대상 제품 삭제."""
    product = db.query(Product).filter(Product.id == product_id).first()
    if not product:
        raise HTTPException(status_code=404, detail="제품을 찾을 수 없습니다.")

    db.delete(product)
    db.commit()
    return MessageResponse(success=True, message="제품이 삭제되었습니다.")
