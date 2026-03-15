"""통합 검색 서비스"""

import logging
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

from domae_mcp.core.crawlers import CrawlerRegistry, SearchResult

logger = logging.getLogger(__name__)


class SearchService:
    """8개 도매상 동시 검색 후 보험코드 기준 그룹핑 반환."""

    def search(
        self,
        keyword: str,
        suppliers: Optional[list[str]] = None,
        credentials: Optional[dict[str, dict[str, str]]] = None,
    ) -> list[dict]:
        """전 도매상 통합 검색.

        Args:
            keyword: 검색 키워드 (제품명 또는 보험코드).
            suppliers: 검색할 도매상 목록. None이면 전체.
            credentials: {도매상명: {"login_id": ..., "login_pw": ...}} 형태.

        Returns:
            보험코드 기준 그룹핑된 검색 결과 리스트.
        """
        if credentials is None:
            credentials = {}

        if not CrawlerRegistry.is_loaded():
            logger.warning("크롤러 미로드 — API 키를 확인하세요.")
            return []

        target_suppliers = suppliers or CrawlerRegistry.list_all()

        all_results: list[SearchResult] = []

        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {}
            for name in target_suppliers:
                cred = credentials.get(name, {})
                if not cred.get("login_id") or not cred.get("login_pw"):
                    logger.debug("계정 없음, 건너뜀: %s", name)
                    continue
                futures[executor.submit(
                    self._search_one, name, keyword, cred
                )] = name

            for future in as_completed(futures):
                name = futures[future]
                try:
                    results = future.result()
                    all_results.extend(results)
                except Exception:
                    logger.warning("크롤러 에러 무시: %s", name, exc_info=True)

        return self._group_by_insurance_code(all_results)

    @staticmethod
    def _search_one(
        supplier_name: str,
        keyword: str,
        cred: dict[str, str],
    ) -> list[SearchResult]:
        """개별 도매상 검색."""
        crawler = CrawlerRegistry.get(supplier_name)
        crawler.ensure_login(cred["login_id"], cred["login_pw"])
        return crawler.search(keyword)

    @staticmethod
    def _group_by_insurance_code(results: list[SearchResult]) -> list[dict]:
        """SearchResult 리스트를 보험코드 기준으로 그룹핑."""
        groups: dict[str, dict] = {}

        for r in results:
            key = r.insurance_code or f"{r.product_name}_{r.unit}"

            if key not in groups:
                groups[key] = {
                    "maker": r.maker,
                    "product_name": r.product_name,
                    "unit": r.unit,
                    "insurance_code": r.insurance_code,
                    "suppliers": [],
                }

            groups[key]["suppliers"].append({
                "name": r.supplier,
                "quantity": r.quantity,
                "price": r.price,
                "product_id": r.product_id,
            })

        return list(groups.values())
