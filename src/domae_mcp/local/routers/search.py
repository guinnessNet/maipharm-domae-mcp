"""검색 라우터: 전 도매상 통합 재고 검색"""

from typing import Optional

from fastapi import APIRouter, Query
from pydantic import BaseModel

from domae_mcp.core.services.search_service import SearchService
from domae_mcp.local.config import ConfigManager

router = APIRouter(tags=["검색"])

_search_service = SearchService()
_config = ConfigManager()


class SupplierItem(BaseModel):
    name: str
    quantity: int
    price: int
    product_id: str


class SearchResultItem(BaseModel):
    maker: str
    product_name: str
    unit: str
    insurance_code: Optional[str] = None
    suppliers: list[SupplierItem]


class SearchResponse(BaseModel):
    keyword: str
    results: list[SearchResultItem]


@router.get("/search", response_model=SearchResponse)
def search(
    keyword: str = Query(..., description="검색 키워드 (제품명 또는 보험코드)"),
    suppliers: Optional[str] = Query(None, description="쉼표 구분 도매상 필터"),
):
    """전 도매상 통합 재고 검색."""
    supplier_list = None
    if suppliers:
        supplier_list = [s.strip() for s in suppliers.split(",") if s.strip()]

    # ConfigManager에서 전체 credentials 가져오기
    from domae_mcp.local.config import SUPPLIERS as ALL_SUPPLIERS

    credentials: dict[str, dict[str, str]] = {}
    for sup in ALL_SUPPLIERS:
        cred = _config.get_credentials(sup)
        if cred.get("login_id") and cred.get("login_pw"):
            credentials[sup] = cred

    results = _search_service.search(
        keyword=keyword,
        suppliers=supplier_list,
        credentials=credentials,
    )

    return SearchResponse(keyword=keyword, results=results)
