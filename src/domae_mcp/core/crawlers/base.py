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
    # - reason_code: 'ok' | 'stock_adjusted' | 'stock_zero' | 'isolated_fail' | 'rejected'
    #                | 'not_sent' | 'send_unknown' | 'other'
    #   not_sent     = 아무것도 전송하지 않았다 (다른 도매로 넘겨도 안전)
    #   rejected     = 전송했고 도매가 받지 않았음이 확인됐다 (다른 도매로 넘겨도 안전)
    #   send_unknown = 전송했으나 접수 여부를 모른다 (어디서도 다시 보내면 안 됨)
    original_quantity: Optional[int] = None
    adjusted_quantity: Optional[int] = None
    available_stock: Optional[int] = None
    reason_code: Optional[str] = None
    # 첫 전송이 아니라 재전송(Phase 2·3, 수량 조정, 품목별 재시도)으로 나온 결과인가 — 알림 표시용
    retried: bool = False
    no_retry: bool = False
    # 성공(stock_adjusted)인데 남은 수량을 재고가 아닌 이유로 보내지 않았을 때 'stopped'.
    # 상위 문구가 '재고 부족' 으로 쓰지 않도록 구분한다(None = 재고 부족 또는 해당 없음).
    shortfall_reason: Optional[str] = None


class CrawlerError(Exception):
    """크롤러 예외"""
    pass


# (연결, 응답) 초. requests 는 timeout 을 주지 않으면 무한 대기한다.
# 도매몰이 TCP 는 받아놓고 응답을 안 주면 그 잡이 영원히 끝나지 않고,
# 워커는 잡을 직렬 처리하므로 전 약국의 검색·주문이 함께 멈춘다.
# (2026-08-06 커넥션 풀 고갈 장애 조사 중 발견한 잠재 정지 경로)
DEFAULT_HTTP_TIMEOUT = (10, 30)


class _TimeoutSession(requests.Session):
    """timeout 을 명시하지 않은 호출에 기본값을 채워 넣는 세션.

    get/post/head 등은 모두 Session.request 를 거치므로 여기 한 곳만 덮으면
    14개 크롤러를 수정하지 않고도 전 HTTP 호출에 상한이 걸린다.
    호출자가 timeout 을 직접 준 경우에는 그 값을 존중한다.
    """

    def request(self, *args, **kwargs):
        if kwargs.get("timeout") is None:
            kwargs["timeout"] = DEFAULT_HTTP_TIMEOUT
        return super().request(*args, **kwargs)


# 크롤러가 판정한 전송 결과는 상위 단계에서 덮지 않는다.
# 특히 send_unknown(접수 여부 불명)을 other 로 바꾸면 DB 에 확정 실패로 남고 상위 재시도가 다시 주문한다.
PRESERVED_REASONS = ("send_unknown", "not_sent", "rejected")


def _keep_or_other(reason):
    return reason if reason in PRESERVED_REASONS else "other"


# rejected는 인천의 명시적 거부 확인 경로에서만 발행한다.
SAFE_RESEND_REASONS = ("not_sent", "rejected")


def checked_qty(value, upper):
    """접수 수량은 범위 안의 정수만 신뢰한다 (bool 제외)."""
    return value if type(value) is int and 0 <= value <= upper else None


def _as_unknown_if_unspecified(result, requested=None, *, allow_zero_adjustment=False):
    """불명확한 결과는 확인 필요로 보존하고 검증 불가 조정 수량을 격리한다."""
    if requested is not None and result.adjusted_quantity is not None:
        adjusted = checked_qty(result.adjusted_quantity, requested)
        if adjusted is None or (result.success and adjusted == 0 and not allow_zero_adjustment):
            import logging
            warning = "접수 조정 수량 검증 실패 — 도매몰 주문내역 확인 필요"
            logging.getLogger(__name__).warning(warning)
            result.message = f"{result.message} — {warning}" if result.message else warning
            result.adjusted_quantity = None
            result.success = False
            result.reason_code = "send_unknown"
    if not result.success and (result.reason_code in (None, "other")
            or (requested is not None and checked_qty(result.fulfilled_quantity, requested) is None)):
        result.reason_code = "send_unknown"
    if requested is not None and not result.success:
        receipt = max(checked_qty(result.adjusted_quantity, requested) or 0,
                      checked_qty(result.fulfilled_quantity, requested) or 0)
        if receipt > 0:
            # 두 필드는 같은 시도의 접수 증거다. 합산하면 실제 주문량을 부풀린다.
            result.fulfilled_quantity = receipt
            result.reason_code = "send_unknown"
    return result


def _settle_resend(result, original_qty, resend_qty):
    result.original_quantity = original_qty
    result.available_stock = resend_qty
    result.retried = True
    # 원래 요청보다 줄여 보냈으므로 접수 증거도 실제 전송량을 넘을 수 없다.
    if checked_qty(result.fulfilled_quantity, resend_qty) is None:
        result.fulfilled_quantity = None
    if not result.success:
        return _as_unknown_if_unspecified(result, resend_qty)
    actual = resend_qty if result.adjusted_quantity is None else checked_qty(result.adjusted_quantity, resend_qty)
    if actual is None or actual == 0:
        import logging
        warning = "재전송 접수 수량 검증 실패 — 도매몰 주문내역 확인 필요"
        logging.getLogger(__name__).warning(warning)
        result.message = f"{result.message} — {warning}" if result.message else warning
        result.success = False
        result.reason_code = "send_unknown"
        result.adjusted_quantity = None
        return result
    result.adjusted_quantity = actual
    result.reason_code = "stock_adjusted"
    result.message = f"재고 부족으로 {original_qty}→{actual}개 조정 주문"
    return result


class BaseCrawler(ABC):
    """도매상 크롤러 기본 클래스.

    모든 크롤러는 이 클래스를 상속하여 login, search, order를 구현.
    세션은 requests.Session으로 관리하며, 메모리에서만 유지.
    """

    SUPPLIER_NAME: str = ""
    SUPPORTS_CART_SYNC: bool = False
    send_guard = None

    def __init__(self):
        self.session = _TimeoutSession()
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

    def order(self, product_id: str, quantity: int, **item) -> OrderResult:
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
            metadata = {k: v for k, v in item.items() if k not in ("product_id", "quantity")}
            r = self.order(pid, qty, **metadata)
            _as_unknown_if_unspecified(r, qty)
            r.original_quantity = qty
            if r.success:
                if r.reason_code is None:
                    r.reason_code = "ok"
                results.append(r)
                continue
            if (r.reason_code not in SAFE_RESEND_REASONS
                    or checked_qty(r.fulfilled_quantity, qty) != 0 or r.no_retry):
                _as_unknown_if_unspecified(r)
                # 접수됐을 수 있다 — 수량을 줄여 다시 보내면 이중 주문이 된다
                results.append(r)
                continue
            # 실패 — 재고 재조회 후 수량 조정 재시도
            stock = self._refetch_stock_for_item(item)
            if stock is not None and 0 < stock < qty:
                r2 = self.order(pid, stock, **metadata)
                _settle_resend(r2, qty, stock)
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
                _as_unknown_if_unspecified(r)
                results.append(r)
        return results

    def presend_stock(self, item: dict) -> Optional[int]:
        """전송 전 검색 후보를 순서대로 조회하고 동일 제품 재고만 반환한다."""
        pid = item.get("product_id") or ""
        code = str(item.get("insurance_code") or "").strip()
        name = (item.get("product_name") or "").strip()
        candidates = ([code] if len(code) == 9 and code.isascii() and code.isdigit() else [])
        candidates += ["".join(name.split()), name, pid]
        seen = set()
        for keyword in candidates:
            if not keyword or keyword in seen:
                continue
            seen.add(keyword)
            try:
                for result in self.search(keyword) or []:
                    if result.product_id != pid:
                        continue
                    local = int(result.local_stock or 0)
                    other = int(result.other_stock or 0)
                    stock = local + other if local or other else int(result.quantity or 0)
                    return max(0, stock)
            except Exception:
                continue
        return None

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

    def _refetch_stock_for_item(self, item: dict) -> Optional[int]:
        """Phase 2 재고 재조회 진입점.

        기본은 refetch_stock(pid, name) 그대로다. 보험코드처럼 item 의 다른 필드로
        더 정확히 찾을 수 있는 크롤러는 이 메서드를 오버라이드한다.
        """
        return self.refetch_stock(item.get("product_id") or "", item.get("product_name", ""))

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

    # True 면 bare 단건 주문이 매 호출마다 장바구니를 저장·비우기·복원한다.
    # 그러면 수량 조정 재시도 전에 래퍼가 장바구니를 비우지 않는다.
    BARE_ORDER_MANAGES_CART: bool = False
    # 재고 이상치 가드 (파싱 버그 방지)
    _MAX_SANE_STOCK = 9999

    def _isolate_unknown_and_resubmit(self, plans, add_fn, submit_fn, clear_fn) -> None:
        """Phase 2 전송이 거부됐을 때 재고를 확인하지 못한 품목(unknown)을 분리해 다시 보낸다.

        all-or-nothing 도매는 품절 품목 하나가 장바구니 전체를 거부한다.
          1) 재고가 확인된 품목(ok/stock_adjusted)만 담아 1회 전송
          2) unknown 품목은 하나씩 단독 전송
        각 묶음의 결과를 plan["final"] 에 남긴다 (accepted / rejected / not_sent / unknown).
        비우기·담기가 실패하면 그 묶음은 보내지 않는다(이전 품목이 섞일 수 있다).
        unknown 이 나오면 접수됐을 수 있으므로 이후 묶음은 보내지 않는다.
        """
        import logging
        _log = logging.getLogger(f"domae.{self.SUPPLIER_NAME or type(self).__name__}")

        unknown = [p for p in plans if p["submit_qty"] > 0 and p["reason_code"] == "unknown"]
        if not unknown:
            return
        known = [p for p in plans
                 if p["submit_qty"] > 0 and p["reason_code"] in ("ok", "stock_adjusted")]

        def _send(group) -> str:
            try:
                clear_fn()
            except Exception as e:
                _log.warning("Phase 3 장바구니 비우기 실패 — 전송 생략: %s", e)
                return "not_sent"
            try:
                for p in group:
                    add_fn(p["item"]["product_id"], p["submit_qty"])
            except Exception as e:
                _log.warning("Phase 3 담기 실패 — 전송 생략: %s", e)
                return "not_sent"
            expected = {}
            for p in group:
                pid = p["item"]["product_id"]
                expected[pid] = expected.get(pid, 0) + p["submit_qty"]
            try:
                r = submit_fn(expected)   # 전송 직전 장바구니 대조 기준 {pid: qty}
            except Exception as e:
                _log.error("Phase 3 전송 예외 — 접수 여부 불명, 이후 전송 중단: %s", e)
                return "unknown"
            if isinstance(r, str):
                return r if r in ("accepted", "rejected", "not_sent", "unknown") else "unknown"
            # bool 만 돌려주는 전송 함수의 False 는 거부인지 결과 불명인지 알 수 없다
            return "accepted" if r else "unknown"

        groups = ([known] if known else []) + [[p] for p in unknown]
        stopped = False
        for group in groups:
            status = "not_sent" if stopped else _send(group)
            stopped = stopped or status == "unknown"
            _log.warning("[Phase 3] %s → %s",
                         ",".join(str(p["item"].get("product_id")) for p in group), status)
            for p in group:
                p["final"] = status

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
                stock = self._refetch_stock_for_item(item)
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
        self, bare_order_fn, product_id: str, quantity: int, product_name: str = "", **item
    ) -> OrderResult:
        """단건 order() 의 Phase 2 wrapper.

        bare_order_fn(pid, qty, **item) → OrderResult — 크롤러 내부의 "순수" order 로직.
        이 wrapper 가 1차 실패 감지 → refetch_stock → 수량 자동 조정 → bare 재호출.

        Group A 크롤러 사용 패턴:
            def order(self, pid, qty, **kwargs):
                return self._order_with_stock_fallback(
                    self._order_bare, pid, qty,
                    **kwargs
                )
        """
        original_qty = int(quantity)
        metadata = dict(item, product_name=product_name)
        r = bare_order_fn(product_id, original_qty, **metadata)
        _as_unknown_if_unspecified(r, original_qty)
        r.original_quantity = original_qty
        if r.success:
            if r.reason_code is None:
                r.reason_code = "ok"
            return r

        if (r.reason_code not in SAFE_RESEND_REASONS
                or checked_qty(r.fulfilled_quantity, original_qty) != 0 or r.no_retry):
            _as_unknown_if_unspecified(r)
            # 접수됐을 수 있다. 수량을 줄여 다시 보내면 이중 주문이 된다.
            return r

        # Phase 2 — 재고 재조회
        import logging
        _logger = logging.getLogger(f"domae.{self.SUPPLIER_NAME or type(self).__name__}")
        _logger.warning("단건 order 실패 → Phase 2 폴백 (pid=%s qty=%d)", product_id, original_qty)

        stock = None
        try:
            stock = self._refetch_stock_for_item(dict(metadata, product_id=product_id, quantity=original_qty))
        except Exception:
            stock = None
        if stock is not None and (stock < 0 or stock > self._MAX_SANE_STOCK):
            stock = None

        if stock is None:
            _logger.warning("Phase 2 재고 조회 불가 — 원래 실패 결과 유지")
            _as_unknown_if_unspecified(r)
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
            _as_unknown_if_unspecified(r)
            r.available_stock = stock
            return r

        # 0 < stock < qty → 수량 조정 재시도
        _logger.warning("Phase 2 수량 조정 재시도: %d → %d (재고 %d)", original_qty, stock, stock)

        # Phase 1 실패로 남은 장바구니 잔존을 정리 → bare 가 saved=[] 를 캡처하도록
        # 크롤러별로 clear 메서드 이름이 다르므로 duck typing 으로 시도
        # 단건 주문이 장바구니를 스스로 저장·비우기·복원하는 크롤러(인천)는 건너뛴다 —
        # 여기서 비우면 첫 시도가 복원한 약국 품목이 지워지고 두 번째 시도가 빈 장바구니를 저장한다.
        for _method_name in (() if self.BARE_ORDER_MANAGES_CART
                             else ("_clear_cart", "_clear_basket", "_clear_temp")):
            clear = getattr(self, _method_name, None)
            if not callable(clear):
                continue
            try:
                import inspect
                if len(inspect.signature(clear).parameters):
                    get_items = getattr(self, "_get_cart_items", None)
                    if not callable(get_items):
                        raise RuntimeError("장바구니 조회 함수 없음")
                    clear(get_items())
                else:
                    clear()
            except Exception:
                return OrderResult(success=False, message="장바구니 비우기 실패 — 전송하지 않음",
                                   original_quantity=original_qty, reason_code="not_sent")
            break

        r2 = bare_order_fn(product_id, stock, **metadata)
        _settle_resend(r2, original_qty, stock)
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

            final = p.get("final")
            if final == "not_sent":
                results.append(OrderResult(
                    success=False, message="장바구니 정리 실패로 전송하지 않음",
                    original_quantity=qty, reason_code="not_sent"))
                continue
            if final == "unknown":
                results.append(OrderResult(
                    success=False, message="전송 결과 확인 불가 — 도매몰 주문내역 확인 필요",
                    original_quantity=qty, reason_code="send_unknown"))
                continue
            if final == "rejected" and rcode == "unknown":
                results.append(OrderResult(
                    success=False, message="단독 전송도 거부됨 — 품절 또는 재고 부족 추정(재고 미확인)",
                    original_quantity=qty, reason_code="isolated_fail"))
                continue

            if final == "rejected":
                results.append(OrderResult(
                    success=False, message="주문 전송 실패 (도매 거부)",
                    original_quantity=qty, reason_code="rejected"))
                continue

            ok = final == "accepted" if final is not None else submit_success
            if not ok:
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
        # 이 함수는 Phase 1 전송이 실패한 뒤에만 불린다 — 여기서 나온 성공은 모두 재전송 결과다
        for r in results:
            if r.success:
                r.retried = True
        return results

def confirmed_quantity(result, requested):
    """DB confirmedQuantity — 접수가 확정된 수량. 모르면 None."""
    if type(requested) is not int or requested <= 0:
        return None
    if result.success:
        if result.adjusted_quantity is None:
            return requested
        return checked_qty(result.adjusted_quantity, requested)
    if result.reason_code == "send_unknown":
        return max(checked_qty(result.fulfilled_quantity, requested) or 0,
                   checked_qty(result.adjusted_quantity, requested) or 0) or None
    return None
