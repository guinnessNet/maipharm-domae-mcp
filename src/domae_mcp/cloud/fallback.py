# src/domae_mcp/cloud/fallback.py
"""품절 품목을 약국의 다음 순번 도매에 즉시 주문한다.

판정은 순수 함수로, 실행(run_fallback)은 DB·락·크롤러를 주입받아 테스트 가능하게 둔다.
같은 약 판정 규칙(2026-10-01 사용자 결정): 보험코드 + 포장(용량·개수 전부)이 같을 때만.

이중 주문 방지 원칙:
  다음 순번으로 넘어가는 경우는 "받지 않았다"가 확실할 때뿐이다.
  - 검색 실패, 후보 없음, 락 실패, pending 기록 실패, 락 갱신 실패 (아무것도 보내지 않음)
  - 크롤러가 reason_code 로 not_sent(사전 거부) 또는 rejected(확정 거부)를 명시한 경우
  그 밖의 실패(예외·모호한 False·결과 기록 실패)는 미확정(unconfirmed)으로 그 품목을 끝낸다.
"""
import logging
import re
from dataclasses import dataclass
from typing import Optional

from domae_mcp.core.crawlers.base import OrderResult, confirmed_quantity, _as_unknown_if_unspecified

logger = logging.getLogger(__name__)

SAFE_TO_CONTINUE = ("not_sent", "rejected")
# 장바구니 처리·알림에서 '확인 필요'로 다루는 결과 (재주문 버튼을 주지 않는다)
NEEDS_CHECK_STATES = ("unconfirmed", "blocked")
BLOCKED_MESSAGE = "이전 주문 결과 확인 전 — 대체 주문 중단"
_INS_CODE = re.compile(r"^[0-9]{9}$")
_UNIT_WORDS = (("캡슐", "c"), ("캅셀", "c"), ("포", "p"), ("정", "t"), ("개", "ea"))
_PACK_TOKEN = re.compile(r"(\d+(?:\.\d+)?)(mg|ml|ea|g|t|c|p|l)")
_PAREN = re.compile(r"\(([^)]*)\)")
_STRENGTH_PREFIX = re.compile(r"^\d+(?:\.\d+)?/(?=\d)")
_SEPARATORS = re.compile(r"[*/x×,+]")


def pack_signature(unit: Optional[str]):
    """포장 표기를 비교 가능한 서명으로 바꾼다. 해석이 불확실하면 None.

    모든 '숫자+단위' 토큰(용량·개수)을 정렬 튜플로 보존한다: '20ml*12P' → ('12p', '20ml').
    숫자가 없는 괄호((병), (PTP), (포))만 부가표기로 보고 버린다. 숫자가 있는 괄호
    ('100T(2병)', '100P(20P*5EA)')는 포장 관계를 해석할 수 없으므로 None.
    복산의 함량 접두('5/500T' 의 '5/')는 뗀다. 토큰·구분자를 빼고도 숫자가 남으면 None.
    """
    if not unit:
        return None
    s = unit.lower()
    if any(re.search(r"\d", inner) for inner in _PAREN.findall(s)):
        return None
    s = _PAREN.sub("", s)
    for word, code in _UNIT_WORDS:
        s = s.replace(word, code)
    s = s.replace(" ", "")
    s = _STRENGTH_PREFIX.sub("", s)
    tokens = []

    def _take(m):
        num = m.group(1)
        if "." in num:
            num = num.rstrip("0").rstrip(".")
        tokens.append(num + m.group(2))
        return ""

    rest = _SEPARATORS.sub("", _PACK_TOKEN.sub(_take, s))
    if not tokens or re.search(r"\d", rest):
        return None
    return tuple(sorted(tokens))


def is_stopped(result) -> bool:
    """재고가 아닌 이유(전송 전 확인 실패·장바구니 고정·잠금 상실·타센터 확인 불가)로 일부만 주문한 결과."""
    return bool(getattr(result, "success", False)) and getattr(result, "shortfall_reason", None) == "stopped"


def stopped_detail(result) -> str:
    """stopped 결과의 남은 수량 설명. 크롤러 메시지 "{n}개 주문 — 남은 …[, m개는 재고 부족]" 의 뒷부분."""
    message = getattr(result, "message", "") or ""
    detail = message.split(" — ", 1)[1] if " — " in message else ""
    return detail if detail.startswith("남은 ") else "나머지는 확인 실패로 주문 안 함"


def unsent_qty(item: dict, result) -> int:
    """stopped 결과에서 확인 실패로 보내지 않은 수량. 크롤러가 값을 주지 않으면 남은 수량 전체로 본다."""
    if not is_stopped(result):
        return 0
    remain = fallback_need_qty(item, result)
    value = getattr(result, "unsent_quantity", None)
    return min(max(int(value), 0), remain) if isinstance(value, int) and not isinstance(value, bool) else remain


def auto_fallback_need(item: dict, result, enabled: bool) -> int:
    """자동주문 대체 수량. 재고가 아닌 이유로 남긴 품목(stopped)은 재고 부족분이 섞여 있어도
    대체주문하지 않고 장바구니에 남긴다(사용자 정책 2026-10-07)."""
    return fallback_need_qty(item, result) if enabled and not is_stopped(result) else 0


def summarize_auto_order(success_items: list, failed_items: list, outcomes: list) -> None:
    """원 주문 결과와 대체주문 결과를 합쳐 품목마다 최종 수량을 한 번만 계산해 품목 dict 에 붙인다.

    ordered_now   원 도매 접수 수량
    fallback_quantity / fallback_supplier   대체주문 접수 수량·도매
    check_quantity / check_supplier         대체주문 결과 확인 필요(unconfirmed) 수량 — 접수됐을 수 있어 남김으로 세지 않는다
    blocked_left / blocked_supplier         대체 도매에 이전 미확정 주문이 있어 보내지 않고 장바구니에 남긴 수량
    left_quantity  장바구니에 남은 수량 = 요청 − 원 접수 − 대체 접수 − 확인 필요
    unsent_left    left 중 재고가 아닌 이유(확인 실패)로 남긴 수량, short_left 는 나머지(재고 부족)
    품목은 원 품목 객체(_src)로 대체주문 결과와 맞춘다."""
    for it, origin_ok in [(i, True) for i in success_items] + [(i, False) for i in failed_items]:
        src = it.get("_src")
        outs = [o for o in outcomes if o.item is src]
        done = [o for o in outs if o.state == "ordered" and o.ordered_qty > 0]
        chk = [o for o in outs if o.state == "unconfirmed"]
        blk = [o for o in outs if o.state == "blocked"]
        requested = int(it.get("requested_quantity") or it.get("quantity") or 0)
        ordered_now = int(it.get("quantity") or 0) if origin_ok else 0
        fb = sum(o.ordered_qty for o in done)
        check = sum(o.need_qty for o in chk)
        left = max(requested - ordered_now - fb - check, 0)
        unsent = min(int(it.get("unsent_quantity") or 0) + sum(o.unsent_qty for o in done), left)
        blocked = min(sum(o.need_qty for o in blk), left - unsent)
        it.update(origin_ok=origin_ok, had_fallback=bool(outs), ordered_now=ordered_now, fallback_quantity=fb,
                  fallback_supplier=", ".join(dict.fromkeys(o.supplier for o in done if o.supplier)) or None,
                  check_quantity=check,
                  check_supplier=", ".join(dict.fromkeys(o.supplier for o in chk if o.supplier)) or None,
                  blocked_left=blocked,
                  blocked_supplier=", ".join(dict.fromkeys(o.supplier for o in blk if o.supplier)) or None,
                  left_quantity=left, unsent_left=unsent, short_left=left - unsent - blocked)


def auto_order_status(success_items: list, failed_items: list, unconfirmed_items: list) -> dict:
    """summarize_auto_order 뒤의 품목들로 자동주문 상태와 알림 집계를 만든다.
    남은 수량이 없으면 success(확인 필요가 있으면 unconfirmed), 남았는데 접수분이 있으면 partial_fail, 없으면 failed."""
    items = list(success_items) + list(failed_items)
    left_any = any(int(i.get("left_quantity", 0)) > 0 for i in items)
    check_items = [i for i in items if int(i.get("check_quantity", 0)) > 0]
    blocked_items = [i for i in items if int(i.get("blocked_left", 0)) > 0]
    needs_check = bool(unconfirmed_items) or bool(check_items) or bool(blocked_items)
    if not left_any:
        status = "unconfirmed" if needs_check else "success"
    elif any(int(i.get("ordered_now", 0)) + int(i.get("fallback_quantity", 0)) > 0 for i in items):
        status = "partial_fail"
    else:
        status = "failed"
    return {
        "status": status,
        "partial": left_any,
        "needs_check": needs_check,
        # 원 주문 실패 품목은 대체주문을 거친 경우에만 남김을 부족·확인 실패로 센다(대체 없는 실패는 실패 건수).
        "shortfall": sum(int(i.get("short_left", 0)) for i in items if i.get("origin_ok") or i.get("had_fallback")),
        "unsent": sum(int(i.get("unsent_left", 0)) for i in items if i.get("origin_ok") or i.get("had_fallback")),
        "fallback_unconfirmed": len(check_items),
        # 이전 미확정 주문 때문에 대체주문을 보내지 않고 장바구니에 남긴 품목·수량(확인 필요와 다르다)
        "fallback_blocked": len(blocked_items),
        "fallback_blocked_qty": sum(int(i.get("blocked_left", 0)) for i in blocked_items),
        "failed_left": sum(1 for i in failed_items if int(i.get("left_quantity", 0)) > 0),
    }


def fallback_need_qty(item: dict, result) -> int:
    """대체 주문할 수량. 재고 0이 확인된 품목은 전량, 수량 조정 성공은 부족분, 나머지 0."""
    qty = int(item.get("quantity") or 1)
    reason = getattr(result, "reason_code", None)
    if not result.success and reason == "stock_zero":
        return qty
    if result.success and reason == "stock_adjusted":
        adj = getattr(result, "adjusted_quantity", None)
        if adj is not None and adj < qty:
            return qty - adj
    return 0


def next_suppliers(supplier_order: list, current: str, available: set) -> list:
    """대체 순서 계약: 저장된 순번에서 current 다음 도매들 중 계정·크롤러가 있는 것, 순서 그대로."""
    if not supplier_order or current not in supplier_order:
        return []
    after = supplier_order[supplier_order.index(current) + 1:]
    return [s for s in after if s in available]


def pick_candidate(results: list, insurance_code: str, unit: str, need_qty: int):
    """보험코드·포장 서명이 같고 재고가 충분한 행 중 재고가 가장 많은 것."""
    want = pack_signature(unit)
    if not insurance_code or want is None:
        return None
    ok = [r for r in results
          if (r.insurance_code or "") == insurance_code
          and pack_signature(r.unit) == want
          and (r.quantity or 0) >= need_qty
          and r.product_id]
    return max(ok, key=lambda r: r.quantity) if ok else None


@dataclass
class FallbackOutcome:
    item: dict
    need_qty: int
    supplier: Optional[str]
    ordered_qty: int
    state: str          # ordered | failed | unconfirmed | blocked | skipped
    message: str
    unsent_qty: int = 0  # ordered 인데 대체 도매가 재고가 아닌 이유(확인 실패)로 남긴 수량

    @property
    def success(self) -> bool:
        return self.state == "ordered"


def _unconfirmed(item, need, sup, ordered, row, record_unconfirmed, message, result=None):
    try:
        ordered = confirmed_quantity(result, need) if result is not None else None
        record_unconfirmed(row, message, confirmed=ordered)
    except Exception as e:
        logger.error("대체주문 미확정 기록 실패 [%s]: %s", sup, e)
    return FallbackOutcome(item, need, sup, ordered or 0, "unconfirmed", message)


def _attempt(item, need, sup, pick, crawler, token, renew_lock,
             record_pending, record_result, record_unconfirmed):
    """한 도매에 한 번 주문을 시도한다.

    None → 받지 않았음이 확실하다. 다음 순번으로 넘어가도 안전하다.
    FallbackOutcome → 이 품목의 대체주문은 여기서 끝난다.
    """
    try:
        row = record_pending(item, sup, pick, need)
    except Exception as e:
        logger.warning("대체주문 pending 기록 실패 [%s] — 전송 안 함: %s", sup, e)
        return None
    if not renew_lock(sup, token):
        logger.warning("대체주문 락 갱신 실패 [%s] — 전송 안 함", sup)
        try:
            record_result(row, item, sup, OrderResult(
                success=False, message="락 갱신 실패 — 전송 안 함", reason_code="not_sent"))
        except Exception as e:
            logger.error("대체주문 not_sent 기록 실패 [%s]: %s", sup, e)
        return None
    try:
        result = crawler.order(pick.product_id, need, product_name=pick.product_name,
                               insurance_code=pick.insurance_code)
        result = _as_unknown_if_unspecified(result, need)
    except Exception as e:
        logger.error("대체주문 전송 예외 [%s] — 접수 여부 불명: %s", sup, e)
        return _unconfirmed(item, need, sup, 0, row, record_unconfirmed,
                            f"{sup} 주문 중 오류 — 접수 여부 확인 필요")

    reason = getattr(result, "reason_code", None)
    if not result.success and reason not in SAFE_TO_CONTINUE:
        return _unconfirmed(item, need, sup, 0, row, record_unconfirmed,
                            f"{sup} 주문 결과 불명({result.message}) — 도매몰 주문내역 확인 필요", result=result)

    ordered = confirmed_quantity(result, need) or 0
    try:
        record_result(row, item, sup, result)
    except Exception as e:
        logger.error("대체주문 결과 기록 실패 [%s] success=%s: %s", sup, result.success, e)
        return FallbackOutcome(item, need, sup, ordered or 0, "unconfirmed",
                               f"{sup} 주문 결과 기록 실패 — 접수 여부 확인 필요")
    if result.success:
        unsent = 0
        if is_stopped(result):
            value = getattr(result, "unsent_quantity", None)
            left = max(need - ordered, 0)
            unsent = min(max(value, 0), left) if isinstance(value, int) and not isinstance(value, bool) else left
        return FallbackOutcome(item, need, sup, ordered, "ordered", f"{sup}에 {ordered}개 대체 주문", unsent)
    logger.info("대체주문 거부 [%s] %s: %s — 다음 순번", sup, reason, result.message)
    return None


def run_fallback(needs, candidates, open_crawler, acquire_lock, renew_lock, release_lock,
                 record_pending, record_result, record_unconfirmed, check_unconfirmed=None, outcomes=None) -> list:
    """check_unconfirmed(supplier, product_id) → None | "same_product" | "other_product".

    같은 약국·도매에 결과 미확정 주문이 있는지 전송(pending 생성) 전에 확인한다.
      same_product   그 품목의 대체주문을 멈춘다(blocked). 다음 순번으로도 넘기지 않는다.
      other_product  장바구니 전체를 보내는 도매(SUPPORTS_CART_SYNC)면 그 도매만 건너뛴다.
      확인 실패      그 도매는 건너뛴다(아무것도 보내지 않았으므로 안전).

    outcomes 를 넘기면 결과를 그 리스트에 바로 쌓는다 — 도중에 예외가 나도 이미 접수된 대체주문 결과를
    호출자가 잃지 않는다(잃으면 접수된 품목이 실패로 보이고 재주문 버튼이 떠 이중 주문이 된다).
    """
    outcomes = [] if outcomes is None else outcomes
    for item, need in needs:
        code = (item.get("insurance_code") or "").strip()
        if not _INS_CODE.match(code) or pack_signature(item.get("unit")) is None:
            outcomes.append(FallbackOutcome(item, need, None, 0, "skipped",
                                            "보험코드·포장단위를 확인할 수 없어 대체 주문하지 않음"))
            continue
        outcome = None
        for sup in candidates:
            try:
                crawler = open_crawler(sup)
                pick = pick_candidate(crawler.search(code), code, item.get("unit"), need)
            except Exception as e:
                logger.warning("대체주문 검색 실패 [%s/%s]: %s", sup, code, e)
                continue
            if not pick:
                continue
            try:
                token = acquire_lock(sup)
            except Exception as e:                          # 락 저장소 장애는 전송 전 — 그 도매만 건너뛴다
                logger.warning("대체주문 락 획득 오류 — %s 건너뜀: %s", sup, e)
                continue
            if token is None:
                logger.warning("대체주문 락 획득 실패 — %s 건너뜀", sup)
                continue
            try:
                if check_unconfirmed is not None:
                    try:
                        prior = check_unconfirmed(sup, pick.product_id)
                    except Exception as e:
                        logger.warning("대체주문 미확정 조회 실패 [%s] — 건너뜀: %s", sup, e)
                        continue
                    if prior == "same_product":
                        logger.warning("대체주문 중단 [%s/%s]: 같은 제품 미확정 주문 존재", sup, pick.product_id)
                        outcome = FallbackOutcome(item, need, sup, 0, "blocked", BLOCKED_MESSAGE)
                        break
                    if prior and getattr(crawler, "SUPPORTS_CART_SYNC", False):
                        logger.warning("대체주문 [%s] 건너뜀: 장바구니 전체 전송 도매에 미확정 주문 존재", sup)
                        continue
                outcome = _attempt(item, need, sup, pick, crawler, token, renew_lock,
                                   record_pending, record_result, record_unconfirmed)
            finally:
                try:
                    release_lock(sup, token)
                except Exception as e:
                    logger.warning("대체주문 락 해제 오류 [%s]: %s", sup, e)
            if outcome is not None:
                break
        outcomes.append(outcome or FallbackOutcome(item, need, None, 0, "failed",
                                                   "다음 순번 도매에 같은 포장의 재고 없음"))
    return outcomes


def cart_action_after_order(item: dict, result):
    """원 주문 직후 장바구니 행 처리.

    delete      전량 주문됨
    keep_failed 일부만 주문됨 — 남은 수량을 실패 상태로 보존
    hold        접수 여부 불명 — 실패 상태로 묶어 자동 재주문을 막고 확인을 요청
    fail        실패
    """
    qty = int(item.get("quantity") or 1)
    if result.success:
        remain = fallback_need_qty(item, result)
        if remain > 0:
            if is_stopped(result):
                return ("keep_failed", remain, f"{qty - remain}개만 주문 — {stopped_detail(result)} — 확인 후 다시 주문하세요")
            return ("keep_failed", remain, f"재고 부족으로 {qty - remain}개만 주문 — 남은 {remain}개")
        return ("delete", 0, "")
    if getattr(result, "reason_code", None) == "send_unknown":
        return ("hold", qty, "전송 결과 확인 필요 — 도매몰 주문내역 확인 후 다시 주문하세요")
    return ("fail", qty, getattr(result, "message", "") or "주문 실패")


def cart_action_after_fallback(outcome: FallbackOutcome):
    """대체주문 뒤 장바구니 행 처리. 행은 원 주문 단계에서 이미 실패 상태(남은 수량)다."""
    if outcome.state == "ordered":
        left = outcome.need_qty - outcome.ordered_qty
        if left <= 0:
            return ("delete", 0, "")
        why = f"{outcome.supplier}에 {outcome.ordered_qty}개 대체주문 — 남은 {left}개"
        if outcome.unsent_qty:
            why += f"(그중 {min(outcome.unsent_qty, left)}개는 확인 실패로 주문 안 함)"
        return ("keep_failed", left, why)
    if outcome.state == "unconfirmed":
        return ("note", 0, f"대체주문 결과 확인 필요({outcome.supplier}) — 도매몰 주문내역을 확인하세요")
    if outcome.state == "blocked":
        return ("note", 0, f"{outcome.supplier} {BLOCKED_MESSAGE} — 도매몰 주문내역을 확인하세요")
    return ("none", 0, "")


def format_fallback_line(o: "FallbackOutcome") -> str:
    """텔레그램 '대체 주문' 줄 — 접수 수량과 그 품목에 남은 수량·사유, 확인 필요면 재주문 금지를 함께 적는다."""
    name = o.item.get("product_name", "")
    if o.state == "ordered":
        text = f"{o.supplier} {o.ordered_qty}개 주문 완료"
        rest = max(o.need_qty - o.ordered_qty, 0)
        u = min(o.unsent_qty, rest)
        if u:
            text += f", {u}개는 확인 실패로 장바구니에 남김"
        if rest - u:
            text += f", {rest - u}개는 장바구니에 남김"
        return f"↪ {name} — {text}"
    if o.state == "unconfirmed":
        return f"⚠ {name} — {o.message} — 도매몰 주문내역 확인 전 재주문 금지"
    if o.state == "blocked":
        return (f"⚠ {name} — {o.supplier}에 이전 미확정 주문이 있어 {o.need_qty}개는 보내지 않음"
                " — 그 주문 확인 전 재주문 금지(장바구니에 남김)")
    return f"✗ {name} — {o.message}"


def format_ordered_line(item: dict) -> str:
    """성공 품목 알림 한 줄. 실제 주문 수량 기준이며, 부족분이 어디로 갔는지(대체주문·확인 필요·장바구니) 적는다."""
    qty = int(item.get("quantity") or 0)
    req = int(item.get("requested_quantity") or qty)
    total = (item.get("price") or 0) * qty
    line = f"• {item.get('product_name', '')} — {qty}개 — {total:,}원"
    fb = int(item.get("fallback_quantity") or 0)
    check = int(item.get("check_quantity") or 0)
    blocked = int(item.get("blocked_left") or 0)
    if "left_quantity" in item:
        left, unsent = int(item["left_quantity"]), int(item.get("unsent_left") or 0)
    else:
        left, unsent = max(req - qty, 0), 0
    if req > qty and item.get("shortfall_detail") and not (fb or check or blocked):
        line += f" (요청 {req}개 — {item['shortfall_detail']}, 장바구니에 남김)"
    elif req > qty and (fb or check or blocked):
        segs = []
        if fb:
            segs.append(f"{item.get('fallback_supplier') or '다른 도매'}에 {fb}개 대체주문")
        if check:
            segs.append(f"{item.get('check_supplier') or '다른 도매'} 대체주문 {check}개 결과 확인 필요"
                        " — 도매몰 주문내역 확인 전 재주문 금지")
        if blocked:
            segs.append(f"{blocked}개는 {item.get('blocked_supplier') or '다른 도매'}에 이전 미확정 주문이 있어 보내지 않음"
                        " — 그 주문 확인 전 재주문 금지(장바구니에 남김)")
        if unsent:
            segs.append(f"{unsent}개는 확인 실패로 장바구니에 남김")
        if left - unsent - blocked:
            segs.append(f"{left - unsent - blocked}개는 장바구니에 남김")
        line += f" (요청 {req}개, 부족 {req - qty}개 중 " + ", ".join(segs) + ")"
    elif req > qty:
        line += f" (요청 {req}개, 부족 {req - qty}개는 장바구니에 남김)"
    if item.get("retried"):
        line += " (재시도 후 주문)"
    return line
