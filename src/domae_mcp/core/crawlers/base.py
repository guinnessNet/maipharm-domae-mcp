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
    # 부분 재고 자동 조정용 (Optional)
    # - original_quantity: 최초 요청 수량
    # - adjusted_quantity: 실제 주문된 수량 (재고 부족으로 축소된 경우)
    # - available_stock: 재조회된 재고
    # - reason_code: 'ok' | 'stock_adjusted' | 'stock_zero' | 'other'
    original_quantity: Optional[int] = None
    adjusted_quantity: Optional[int] = None
    available_stock: Optional[int] = None
    reason_code: Optional[str] = None


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
        기본 구현은 order()를 순차 호출. 크롤러별로 오버라이드하여 일괄 처리 가능.

        실패 시 refetch_stock() 이 지원되면 재고 재조회 후 수량 하향 재시도.
        """
        results = []
        for item in items:
            pid = item["product_id"]
            qty = item["quantity"]
            name = item.get("product_name", "")
            r = self.order(pid, qty)
            r.original_quantity = qty
            if r.success:
                if r.reason_code is None:
                    r.reason_code = "ok"
                results.append(r)
                continue
            # 실패 — 재고 재조회 후 수량 조정 재시도
            stock = self.refetch_stock(pid, name)
            if stock is not None and 0 < stock < qty:
                r2 = self.order(pid, stock)
                r2.original_quantity = qty
                r2.adjusted_quantity = stock
                r2.available_stock = stock
                r2.reason_code = "stock_adjusted" if r2.success else "other"
                if r2.success and not r2.message:
                    r2.message = f"재고 부족으로 {qty}→{stock}개 조정 주문"
                results.append(r2)
            elif stock == 0:
                results.append(OrderResult(
                    success=False,
                    message=f"재고 0 — 주문 누락",
                    original_quantity=qty,
                    adjusted_quantity=0,
                    available_stock=0,
                    reason_code="stock_zero",
                ))
            else:
                r.reason_code = "other"
                results.append(r)
        return results

    def refetch_stock(self, product_id: str, product_name: str = "") -> Optional[int]:
        """재고 재조회 훅.

        기본 구현: product_name 으로 self.search() 호출 후 product_id 일치 항목의
        quantity 반환. product_name 이 없으면 None. 검색 실패/미일치 시 None.

        크롤러별로 더 정확한 엔드포인트(상품 상세 등)가 있으면 오버라이드.

        반환:
          None  → 재고 정보 없음 (수량 조정 폴백 생략, 기존 수량으로 재시도만)
          0     → 재고 없음 (stock_zero)
          N>0   → 가용 재고 N
        """
        if not product_name:
            return None
        try:
            results = self.search(product_name)
        except Exception:
            return None
        if not results:
            return None
        for r in results:
            if getattr(r, "product_id", "") == product_id:
                # 지오영 등 다지점은 local+other 합산이 총 재고
                qty = int(getattr(r, "quantity", 0) or 0)
                local = int(getattr(r, "local_stock", 0) or 0)
                other = int(getattr(r, "other_stock", 0) or 0)
                if local or other:
                    return max(0, local + other)
                return max(0, qty)
        return None

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


class PartialStockFallbackMixin:
    """All-or-Nothing 방식 batch 크롤러용 Phase 2 폴백.

    전략: 각 품목 재고 재조회 → 조정된 수량으로 장바구니 재구성 → 1회 submit.
    (단건 반복보다 submit 횟수가 N배 적어 비용 유리.)

    사용 흐름 (크롤러 order_batch 내부):
      1. plans = self._compute_adjusted_plan(items)
      2. self._clear_cart()
      3. for p in plans:
           if p["submit_qty"] > 0:
               self._add_to_cart(pid, p["submit_qty"], ...)
      4. submit_success = self._submit_order() if any submit_qty > 0 else False
      5. return self._build_results_from_plan(plans, submit_success)

    plan dict 구조:
      - item: 원본 item dict
      - submit_qty: int (0 = 장바구니에서 제외)
      - reason_code: 'ok' | 'stock_adjusted' | 'stock_zero' | 'unknown'
      - available_stock: int | None
    """

    # 재고 이상치 가드 (파싱 버그 방지)
    _MAX_SANE_STOCK = 9999

    def _compute_adjusted_plan(self, items: list[dict]) -> list[dict]:
        """각 품목 재고 재조회 후 조정 계획 생성.

        submit_qty=0 → 장바구니에서 제외 (재고 0 또는 product_id 누락)
        submit_qty>0 → 해당 수량으로 장바구니 담기
        """
        plans = []
        for item in items:
            pid = item.get("product_id") or ""
            qty = int(item.get("quantity") or 1)
            name = item.get("product_name", "")

            if not pid:
                plans.append({
                    "item": item, "submit_qty": 0,
                    "reason_code": "other", "available_stock": None,
                    "error_message": "product_id 누락",
                })
                continue

            stock = None
            try:
                stock = self.refetch_stock(pid, name)
            except Exception:
                stock = None

            if stock is not None and (stock < 0 or stock > self._MAX_SANE_STOCK):
                stock = None

            if stock is None:
                # 재고 조회 불가 — 원래 수량 그대로 재시도 (수량 조정 X)
                plans.append({
                    "item": item, "submit_qty": qty,
                    "reason_code": "unknown", "available_stock": None,
                })
            elif stock == 0:
                plans.append({
                    "item": item, "submit_qty": 0,
                    "reason_code": "stock_zero", "available_stock": 0,
                })
            elif stock >= qty:
                plans.append({
                    "item": item, "submit_qty": qty,
                    "reason_code": "ok", "available_stock": stock,
                })
            else:
                plans.append({
                    "item": item, "submit_qty": stock,
                    "reason_code": "stock_adjusted", "available_stock": stock,
                })
        return plans

    def _order_with_stock_fallback(
        self, bare_order_fn, product_id: str, quantity: int, product_name: str = ""
    ) -> OrderResult:
        """단건 order() 의 Phase 2 wrapper.

        bare_order_fn(pid, qty) → OrderResult — 크롤러 내부의 "순수" order 로직.
        이 wrapper 가 1차 실패 감지 → refetch_stock → 수량 자동 조정 → bare 재호출.

        Group A 크롤러 사용 패턴:
            def order(self, pid, qty, **kwargs):
                return self._order_with_stock_fallback(
                    self._order_bare, pid, qty,
                    product_name=kwargs.get("product_name", "")
                )
        """
        original_qty = int(quantity)
        r = bare_order_fn(product_id, original_qty)
        r.original_quantity = original_qty
        if r.success:
            if r.reason_code is None:
                r.reason_code = "ok"
            return r

        # Phase 2 — 재고 재조회
        import logging
        _logger = logging.getLogger(f"domae.{self.SUPPLIER_NAME or type(self).__name__}")
        _logger.warning("단건 order 실패 → Phase 2 폴백 (pid=%s qty=%d)", product_id, original_qty)

        stock = None
        try:
            stock = self.refetch_stock(product_id, product_name)
        except Exception:
            stock = None
        if stock is not None and (stock < 0 or stock > self._MAX_SANE_STOCK):
            stock = None

        if stock is None:
            _logger.warning("Phase 2 재고 조회 불가 — 원래 실패 결과 유지")
            r.reason_code = "other"
            return r
        if stock == 0:
            _logger.warning("Phase 2 재고 0 — stock_zero")
            return OrderResult(
                success=False,
                message="재고 0 — 주문 누락",
                original_quantity=original_qty,
                adjusted_quantity=0,
                available_stock=0,
                reason_code="stock_zero",
            )
        if stock >= original_qty:
            # 재고 충분한데 실패 → 재고 외 원인. 그대로 실패 반환.
            _logger.warning("Phase 2 재고 %d ≥ 요청 %d — 재고 원인 아님, 재시도 스킵", stock, original_qty)
            r.reason_code = "other"
            r.available_stock = stock
            return r

        # 0 < stock < qty → 수량 조정 재시도
        _logger.warning("Phase 2 수량 조정 재시도: %d → %d (재고 %d)", original_qty, stock, stock)

        # Phase 1 실패로 남은 장바구니 잔존을 정리 → bare 가 saved=[] 를 캡처하도록
        # 크롤러별로 clear 메서드 이름이 다르므로 duck typing 으로 시도
        for _method_name in ("_clear_cart", "_clear_basket", "_clear_temp"):
            _m = getattr(self, _method_name, None)
            if callable(_m):
                try:
                    _m()
                    break
                except TypeError:
                    # familypharm 처럼 items 인자를 요구하는 경우
                    try:
                        _items_fn = getattr(self, "_get_cart_items", None)
                        if callable(_items_fn):
                            _m(_items_fn())
                            break
                    except Exception:
                        pass
                except Exception:
                    pass

        r2 = bare_order_fn(product_id, stock)
        r2.original_quantity = original_qty
        r2.adjusted_quantity = stock
        r2.available_stock = stock
        r2.reason_code = "stock_adjusted" if r2.success else "other"
        if r2.success:
            # Mixin 이 수량 조정을 감지했음을 명시적으로 메시지에 반영 (bare 가 채운 "주문 전송 완료" 덮어씀)
            r2.message = f"재고 부족으로 {original_qty}→{stock}개 조정 주문"
        _logger.warning("Phase 2 재시도 결과: %s", r2.success)
        return r2

    def _build_results_from_plan(
        self, plans: list[dict], submit_success: bool
    ) -> list[OrderResult]:
        """계획 + submit 결과 → 품목별 OrderResult.

        submit_success=True  → submit_qty>0 품목은 성공 (수량 조정 여부에 따라 reason_code 결정)
        submit_success=False → submit_qty>0 품목은 "조정 후 재시도 실패"
        submit_qty=0 품목은 항상 submit 결과와 무관하게 stock_zero/other 처리
        """
        results: list[OrderResult] = []
        for p in plans:
            item = p["item"]
            qty = int(item.get("quantity") or 1)
            rcode = p["reason_code"]
            stock = p["available_stock"]
            submit_qty = p["submit_qty"]

            # 장바구니 제외 품목 (재고 0 또는 사전 실패)
            if submit_qty == 0:
                if rcode == "stock_zero":
                    results.append(OrderResult(
                        success=False,
                        message="재고 0 — 주문 누락",
                        original_quantity=qty,
                        adjusted_quantity=0,
                        available_stock=0,
                        reason_code="stock_zero",
                    ))
                else:
                    results.append(OrderResult(
                        success=False,
                        message=p.get("error_message") or "주문 불가",
                        original_quantity=qty,
                        reason_code="other",
                    ))
                continue

            # 장바구니에 담긴 품목 — submit 결과에 따라 성패 분기
            if not submit_success:
                results.append(OrderResult(
                    success=False,
                    message="주문 전송 실패 (수량 조정 후 재시도)",
                    original_quantity=qty,
                    adjusted_quantity=submit_qty if rcode == "stock_adjusted" else None,
                    available_stock=stock,
                    reason_code="other",
                ))
            elif rcode == "stock_adjusted":
                results.append(OrderResult(
                    success=True,
                    message=f"재고 부족으로 {qty}→{submit_qty}개 조정 주문",
                    original_quantity=qty,
                    adjusted_quantity=submit_qty,
                    available_stock=stock,
                    reason_code="stock_adjusted",
                ))
            else:
                # ok 또는 unknown (재고 조회 불가했지만 submit 성공)
                results.append(OrderResult(
                    success=True,
                    message="주문 전송 완료",
                    original_quantity=qty,
                    available_stock=stock,
                    reason_code="ok",
                ))
        return results
