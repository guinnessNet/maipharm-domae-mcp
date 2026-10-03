"""순수 로직: 긴급 검색어 생성, 품목 매칭, 단일 도매 결과 판정."""

import logging
import math
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from numbers import Real
from typing import Optional

from domae_mcp.core.crawlers.base import OrderResult, checked_qty, _as_unknown_if_unspecified

logger = logging.getLogger(__name__)

_INS_CODE = re.compile(r"^[0-9]{9}$")
_INTEGER_TEXT = re.compile(r"^[+-]?[0-9]+$")
_QTY_TOKEN = re.compile(r"^\d")
_LEADING_MARKS = re.compile(r"^[\$`'\s]+")
_CAPITAL_BEFORE_KOREAN = re.compile(r"^[A-Z](?=[가-힣])")
_PAREN_TAIL = re.compile(r"\(.*$")
_MAKERS = frozenset(("삼아", "한화", "대웅", "동아", "종근당", "유한", "한미", "보령", "일동", "광동",
                     "녹십자", "JW", "중외", "삼일", "한독", "부광", "경동", "동국", "명인", "환인"))
_SAFE_SKIP = frozenset(("not_sent", "stock_zero"))


def _keyword_add(values: list[str], value: Optional[str]) -> None:
    value = (value or "").strip()
    if value and not _QTY_TOKEN.match(value) and value not in values:
        values.append(value)


def urgent_keywords(product_name: Optional[str], insurance_code: Optional[str]) -> list[str]:
    """보험코드와 제품명 변형을 중복 없이 검색 우선순위대로 반환한다."""
    keywords: list[str] = []
    code = (insurance_code or "").strip()
    if _INS_CODE.fullmatch(code):
        keywords.append(code)

    original = (product_name or "").strip()
    if not original:
        return keywords

    body = _CAPITAL_BEFORE_KOREAN.sub("", _LEADING_MARKS.sub("", original))
    tokens = body.split()
    if len(tokens) > 1 and tokens[0] in _MAKERS:
        tokens = tokens[1:]
    if tokens:
        core_head = tokens[0].split("/", 1)[0]
        _keyword_add(keywords, _PAREN_TAIL.sub("", core_head))

    slash_head = body.split("/", 1)[0]
    normalized_head = re.sub(r"\s+", "", slash_head)
    normalized_head = _PAREN_TAIL.sub("", normalized_head)
    _keyword_add(keywords, normalized_head)
    _keyword_add(keywords, slash_head)
    _keyword_add(keywords, original)
    return keywords


def find_listing(crawler, product_id: str, keywords: list[str]):
    """키워드별 검색 실패를 건너뛰고 정확히 같은 도매 품목코드만 반환한다."""
    for keyword in keywords:
        try:
            results = crawler.search(keyword) or []
            for result in results:
                if result.product_id == product_id:
                    return result
        except Exception as exc:
            logger.info("긴급 검색 실패 kw=%r: %s — 다음 검색어", keyword, exc)
    return None


@dataclass
class UrgentStep:
    state: str
    qty: int
    message: str
    price: int = 0
    fulfilled: int = 0


def _integer_quantity(value):
    """정수로 손실 없이 표현된 수량만 반환한다. bool 및 분수는 거부한다."""
    if isinstance(value, bool):
        return None
    if type(value) is int:
        return value
    if isinstance(value, str):
        stripped = value.strip()
        if not _INTEGER_TEXT.fullmatch(stripped):
            return None
        try:
            return int(stripped)
        except ValueError:
            return None
    if isinstance(value, Decimal):
        if not value.is_finite() or value != value.to_integral_value():
            return None
        return int(value)
    if isinstance(value, Real):
        try:
            if isinstance(value, float) and not math.isfinite(value):
                return None
            parsed = int(value)
            return parsed if value == parsed else None
        except (OverflowError, TypeError, ValueError):
            return None
    return None


def _safe_price(value) -> int:
    try:
        parsed = _integer_quantity(value)
        return parsed if parsed is not None else 0
    except Exception:
        return 0


def urgent_supplier_step(crawler, product_id, keywords, need, *, before_send, reject_reason=None):
    """도매 한 곳을 확인하고 안전한 다음 상태를 반환한다.

    skip은 검색 실패, 재고 없음, 사전 거절, 또는 전송하지 않았음이 확인된
    not_sent/stock_zero 결과에만 사용한다. 접수 확정 수량이 있는 실패는 halt에
    그 수량을 보존하고, 그 외 주문 결과가 불명확하면 halt한다.
    """
    listing = find_listing(crawler, product_id, keywords)
    if listing is None:
        return UrgentStep("skip", 0, "검색 매칭 실패")

    if reject_reason:
        return UrgentStep("skip", 0, str(reject_reason))

    stock = _integer_quantity(getattr(listing, "quantity", None))
    if stock is not None and stock <= 0:
        return UrgentStep("skip", 0, "재고 없음")
    if stock is None:
        return UrgentStep("skip", 0, "재고 수량 이상 — 주문 전 중단")

    order_need = _integer_quantity(need)
    if order_need is None or order_need < 0:
        return UrgentStep("skip", 0, "요청 수량 이상 — 주문 전 중단")
    if order_need == 0:
        return UrgentStep("skip", 0, "요청 수량 없음")

    order_qty = min(order_need, stock)
    # Claim 실패는 주문 결과 해석 경계 밖에서 전파되어야 한다.
    before_send()
    try:
        result = crawler.order(
            product_id,
            order_qty,
            product_name=listing.product_name,
            insurance_code=getattr(listing, "insurance_code", None),
        )
        if not isinstance(result, OrderResult):
            raise TypeError(f"주문 결과 형식 이상: {type(result).__name__}")
        result = _as_unknown_if_unspecified(result, order_qty)

        if result.success:
            actual = order_qty if result.adjusted_quantity is None else checked_qty(result.adjusted_quantity, order_qty)
            if actual is None or actual == 0:
                return UrgentStep("halt", 0, "체결 수량 이상 — 도매몰 주문내역 확인 필요")
            try:
                raw_price = getattr(listing, "price", None)
            except Exception:
                raw_price = None
            price = _safe_price(raw_price)
            return UrgentStep("filled", actual, result.message or "주문 완료", price=price)

        fulfilled = checked_qty(getattr(result, "fulfilled_quantity", None), order_qty)
        if result.reason_code in _SAFE_SKIP and fulfilled == 0:
            return UrgentStep("skip", 0, result.message or "주문 안 됨")
        if fulfilled is None:
            note = " (보고 수량 이상)"
            confirmed = 0
        else:
            note = ""
            confirmed = fulfilled
        message = f"주문 결과 불명({result.message}){note} — 도매몰 주문내역 확인 필요"
        return UrgentStep("halt", 0, message, fulfilled=confirmed)
    except Exception as exc:
        logger.error("긴급 주문 결과 불명 pid=%s: %s", product_id, exc)
        return UrgentStep("halt", 0, f"주문 중 오류 — 접수 여부 확인 필요 ({exc})")
