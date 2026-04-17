"""크롤러 기본 클래스 및 데이터 모델"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional

import requests
from bs4 import BeautifulSoup


@dataclass
class SearchResult:
    """검색 결과 단건"""
    maker: str = ""
    product_name: str = ""
    unit: str = ""
    insurance_code: str = ""
    quantity: int = 0
    price: int = 0
    supplier: str = ""
    product_id: str = ""
    # 다지점 도매 (지오영/복산 등)의 센터별 재고 분리.
    # 기본값 0/""로 두면 단일센터 도매는 기존 동작 그대로.
    local_stock: int = 0
    other_stock: int = 0
    other_move_code: str = ""


@dataclass
class OrderResult:
    """주문 결과"""
    success: bool = False
    message: str = ""
    order_id: str = ""
    # 분할 주문용. 단일 주문은 fulfilled=quantity, failed=0 으로 채움.
    fulfilled_quantity: int = 0
    failed_quantity: int = 0


class CrawlerError(Exception):
    """크롤러 예외"""
    pass


class BaseCrawler(ABC):
    """도매상 크롤러 기본 클래스.

    모든 크롤러는 이 클래스를 상속하여 login, search, order를 구현.
    세션은 requests.Session으로 관리하며, 메모리에서만 유지.
    """

    SUPPLIER_NAME: str = ""
    SUPPORTS_CART_SYNC: bool = False

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        })
        self._logged_in = False

    @abstractmethod
    def login(self, login_id: str, login_pw: str) -> bool:
        """로그인. 성공 시 True 반환."""
        ...

    @abstractmethod
    def search(self, keyword: str) -> list[SearchResult]:
        """키워드 검색. 결과 리스트 반환."""
        ...

    def order(self, product_id: str, quantity: int) -> OrderResult:
        """주문 실행. 미구현 크롤러는 기본 실패 반환."""
        return OrderResult(success=False, message="주문 미지원 도매상입니다.")

    def order_batch(self, items: list[dict]) -> list[OrderResult]:
        """복수 품목 일괄 주문. items: [{"product_id": str, "quantity": int}, ...]
        기본 구현은 order()를 순차 호출. 크롤러별로 오버라이드하여 일괄 처리 가능."""
        return [self.order(item["product_id"], item["quantity"]) for item in items]

    def get_cart(self) -> list[dict]:
        """장바구니 조회. 미구현 크롤러는 빈 리스트 반환."""
        return []

    def ensure_login(self, login_id: str, login_pw: str) -> bool:
        """로그인 상태 확인 후 필요 시 로그인. 실패 시 CrawlerError 발생."""
        if self._logged_in:
            return True
        if not self.login(login_id, login_pw):
            raise CrawlerError(f"{self.SUPPLIER_NAME or type(self).__name__} 로그인 실패")
        self._logged_in = True
        return True

    def _soup(self, html: str, parser: str = "lxml") -> BeautifulSoup:
        """HTML → BeautifulSoup"""
        return BeautifulSoup(html, parser)

    def _safe_int(self, value: str, default: int = 0) -> int:
        """문자열 → int (쉼표, 공백 제거)"""
        if not value:
            return default
        try:
            return int(str(value).replace(",", "").replace(" ", "").strip())
        except (ValueError, TypeError):
            return default
