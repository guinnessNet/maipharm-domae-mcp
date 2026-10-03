# src/domae_mcp/core/crawlers/cart_snapshot.py
"""약국 장바구니 보존 — 정확 일치 규칙(사용자 결정 2026-10-03).

도매몰 장바구니는 계정당 하나다. 누가 담았는지 추정하지 않는다.
- 모든 장바구니 조작(비우기·담기) 직전에 ① 실행 잠금을 아직 내가 갖고 있는지 ② 장바구니가 예상 상태와
  정확히 같은지 확인한다. 한 번이라도 다르면 그 실행은 '고정(frozen)'되어 이후 어떤 조작도 하지 않는다.
- 아직 장바구니를 건드리기 전에 다름을 발견하면 그냥 그만둔다(약사 변경은 약사 의도 — 복원하지 않는다).
- 워커가 중간에 죽은 경우 자동 복구하지 않는다. 장바구니가 이미 원래 상태와 같을 때만 기록을 정리한다.
- 해제는 '장바구니 확인 완료' 버튼으로만. 기록·차단에는 만료(TTL)가 없다.
Redis 쓰기는 모두 WATCH/MULTI 트랜잭션으로 소유자·차수를 비교한 뒤에만 한다.

크롤러가 구현하는 함수: _cart_map() -> dict(판독 실패 시 예외), _cart_add_raw(key, qty)(담기 요청 1건),
그리고 _cart_delete_raw(key)(품목 하나 삭제 요청 1건) 또는 — 전체 비우기가 요청 1건뿐인 도매는 —
_cart_clear_raw(). 판독·대조는 이 모듈이 한다. 여러 요청을 묶어 한 번에 지우지 않는다.
"""
import hashlib
import base64
import json
import logging
import uuid
import time
from datetime import datetime, timezone

logger = logging.getLogger(__name__)
LOCK_TTL = 900          # 실행 잠금. 조작 직전마다 갱신한다
RELEASE_PENDING_KEY = "domae:cart_release_pending"
REISSUE_SEC = 3600      # 막힌 상태에서 확인 버튼 재발급 간격


def _notify(monitor_id, text, reply_markup=None):
    from domae_mcp.cloud.notifier import Notifier
    Notifier.notify_monitor(monitor_id, text, reply_markup=reply_markup)


def _enc(d: dict) -> list:
    return [[list(k) if isinstance(k, tuple) else k, q] for k, q in d.items()]


def _dec(rows) -> dict:
    return {tuple(k) if isinstance(k, list) else k: q for k, q in rows}


def _s(v):
    return v.decode() if isinstance(v, bytes) else v


class CartChanged(Exception):
    """장바구니가 예상과 다르거나 실행 잠금을 잃음 — 더 조작하지 않는다."""


class CartSnapshot:
    """도매 계정 장바구니 단위 기록(같은 계정을 쓰는 모니터끼리 공유).

    잠금  domae:cart_lock:{supplier}:{acct}      = 실행 ID (EX LOCK_TTL, 조작 직전 갱신)
    기록  domae:cart_snapshot:{supplier}:{acct}  = {"run", "rev", "snap"}   (만료 없음)
    실패  domae:cart_restore_failed:{...}        = {"rev", "reason", "snap"} (만료 없음)
    """

    def __init__(self, redis, monitor_id, supplier, account=""):
        acct = hashlib.sha1(account.encode()).hexdigest()[:16] if account else f"m-{monitor_id}"
        identity = "account:" + account if account else "monitor:" + monitor_id
        self.account_binding = base64.urlsafe_b64encode(hashlib.sha256(identity.encode()).digest()[:12]).decode()
        self._r, self._m, self._s = redis, monitor_id, supplier
        base = f"{supplier}:{acct}"
        self.lock_key = f"domae:cart_lock:{base}"
        self.key = f"domae:cart_snapshot:{base}"
        self.failed_key = f"domae:cart_restore_failed:{base}"
        self.rev_key = f"domae:cart_rev:{base}"
        self.reissue_key = f"domae:cart_reissue:{base}"
        self.run_id = uuid.uuid4().hex
        self.rev = None                                  # 이 실행이 저장한 기록 차수

    # ── 트랜잭션 도우미 ──
    def _tx(self, fn, *keys):
        """WATCH keys → fn(pipe) 가 조건을 확인하고 MULTI 이후 명령을 넣는다. 경합 시 재시도."""
        return self._r.transaction(fn, *keys, value_from_callable=True)

    def _rec(self, pipe):
        raw = pipe.get(self.key)
        return None if raw is None else json.loads(raw)

    # ── 잠금 ──
    def lock(self) -> bool:
        def fn(pipe):
            if pipe.exists(self.lock_key):
                return False
            pipe.multi()
            pipe.set(self.lock_key, self.run_id, ex=LOCK_TTL)
            return True
        return self._tx(fn, self.lock_key)

    def owned(self) -> bool:
        """아직 내 잠금인지 확인하고 만료를 연장한다(원자적)."""
        def fn(pipe):
            if _s(pipe.get(self.lock_key)) != self.run_id:
                return False
            rec = self._rec(pipe)
            if self.rev is not None and (not rec or rec.get("run") != self.run_id or rec.get("rev") != self.rev):
                return False
            pipe.multi()
            pipe.expire(self.lock_key, LOCK_TTL)
            return True
        return self._tx(fn, self.lock_key, self.key)

    def unlock(self):
        def fn(pipe):
            rec = self._rec(pipe)
            if self.rev is not None and rec and (rec.get("run") != self.run_id or rec.get("rev") != self.rev):
                return
            if _s(pipe.get(self.lock_key)) == self.run_id:
                pipe.multi()
                pipe.delete(self.lock_key)
        self._tx(fn, self.lock_key, self.key)

    # ── 기록 ──
    def load(self):
        raw = self._r.get(self.key)
        return None if raw is None else json.loads(raw)

    @staticmethod
    def snap_of(rec) -> dict:
        return _dec(rec["snap"])

    def save(self, snap: dict) -> int:
        """내 잠금 아래에서만, 기록이 없을 때만 저장."""

        def fn(pipe):
            if _s(pipe.get(self.lock_key)) != self.run_id or pipe.exists(self.key):
                raise CartChanged("기록 저장 조건 불충족(잠금 상실 또는 기록 존재)")
            rev = int(pipe.get(self.rev_key) or 0) + 1
            pipe.multi()
            pipe.set(self.rev_key, rev)
            pipe.set(self.key, json.dumps({"run": self.run_id, "rev": rev, "snap": _enc(snap)}, ensure_ascii=False))
            return rev
        rev = self._tx(fn, self.lock_key, self.key, self.rev_key)
        self.rev = rev
        return rev

    def done(self):
        """복원 완료 — 내 기록(run·rev 일치)일 때만 지우고, 같은 차수 실패 표시도 함께 지운다."""
        def fn(pipe):
            rec = self._rec(pipe)
            if (_s(pipe.get(self.lock_key)) != self.run_id or
                    not rec or rec.get("run") != self.run_id or rec.get("rev") != self.rev):
                return
            failed = pipe.get(self.failed_key)
            pipe.multi()
            pipe.delete(self.key)
            if failed and json.loads(failed).get("rev") == self.rev:
                pipe.delete(self.failed_key)
        self._tx(fn, self.lock_key, self.key, self.failed_key)

    def clear_stale(self, rev):
        """남은 기록인데 장바구니가 이미 원래와 같음 — 그 차수 기록일 때만 정리."""
        def fn(pipe):
            rec = self._rec(pipe)
            if _s(pipe.get(self.lock_key)) != self.run_id or not rec or rec.get("rev") != rev:
                raise CartChanged("남은 기록이 바뀜")
            failed = pipe.get(self.failed_key)
            pipe.multi()
            pipe.delete(self.key)
            if failed and json.loads(failed).get("rev") == rev:
                pipe.delete(self.failed_key)
        self._tx(fn, self.lock_key, self.key, self.failed_key)
        logger.info("[%s] 남은 장바구니 기록 정리 rev=%s (장바구니가 원래와 같음)", self._s, rev)

    def restore_failed(self, reason: str):
        """내 기록(run·rev 일치)일 때만 실패 표시를 남기고 확인 버튼을 보낸다. 기록은 지우지 않는다."""
        def fn(pipe):
            rec = self._rec(pipe)
            if (_s(pipe.get(self.lock_key)) != self.run_id or
                    not rec or rec.get("run") != self.run_id or rec.get("rev") != self.rev):
                return None
            pipe.multi()
            pipe.set(self.reissue_key, str(self.rev), ex=REISSUE_SEC)
            pipe.set(self.failed_key, json.dumps({"rev": self.rev, "reason": reason, "snap": rec["snap"]},
                                                 ensure_ascii=False))
            return rec
        rec = self._tx(fn, self.lock_key, self.key, self.failed_key, self.reissue_key)
        if rec:
            self._send_button(self.rev, reason, self.snap_of(rec))

    def reissue(self, rec):
        """막힌 상태로 진입 시 같은 차수 확인 버튼을 다시 보낸다(1시간에 1회)."""
        def fn(pipe):
            current = self._rec(pipe)
            if (_s(pipe.get(self.lock_key)) != self.run_id or current != rec or
                    _s(pipe.get(self.reissue_key)) == str(rec["rev"])):
                return None
            failed = pipe.get(self.failed_key)
            reason = json.loads(failed)["reason"] if failed else "이전 주문 도중 중단됨(워커 종료 가능)"
            pipe.multi()
            pipe.set(self.reissue_key, str(rec["rev"]), ex=REISSUE_SEC)
            return reason
        reason = self._tx(fn, self.lock_key, self.key, self.failed_key, self.reissue_key)
        if reason is not None:
            self._send_button(rec["rev"], reason, self.snap_of(rec))

    def _send_button(self, rev, reason, snap):
        names = ", ".join(str(k) for k in list(snap)[:5]) or "(빈 장바구니)"
        button = {"inline_keyboard": [[{"text": "장바구니 확인 완료",
                                        "callback_data": f"CR:{self._m[:8]}:{self._s[:10]}:{rev}:{self.account_binding}"}]]}
        try:
            _notify(self._m, f"⚠ [{self._s}] {reason}\n원래 장바구니: {names}\n도매몰 장바구니를 확인·정리한 뒤 "
                             f"아래 버튼을 눌러 주세요. 누르기 전까지 이 도매 자동주문은 멈춥니다.", button)
        except Exception as e:
            logger.error("확인 요청 알림 실패: %s", e)

    def release_key(self, rev):
        return self.key.replace("domae:cart_snapshot:", "domae:cart_release:", 1) + f":{rev}"

    @staticmethod
    def release_receipt(redis_client, key):
        raw = redis_client.get(key)
        return json.loads(raw) if raw is not None else None

    @staticmethod
    def ack_release(redis_client, key, receipt):
        """DB 커밋 확인 이후에만 호출: 같은 영수증만 ACK한다."""
        def fn(pipe):
            raw = pipe.get(key)
            if raw is None or json.loads(raw) != receipt:
                return False
            pipe.multi()
            pipe.delete(key)
            pipe.zrem(RELEASE_PENDING_KEY, key)
            return True
        return redis_client.transaction(fn, key, value_from_callable=True)

    @staticmethod
    def defer_release(redis_client, key):
        """실패한 영수증은 내용·시각을 유지하고 대기열 맨 뒤로 옮긴다."""
        def fn(pipe):
            if not pipe.exists(key) or pipe.zscore(RELEASE_PENDING_KEY, key) is None:
                return False
            tail = pipe.zrevrange(RELEASE_PENDING_KEY, 0, 0, withscores=True)
            score = max(time.time(), tail[0][1] + 1) if tail else time.time()
            pipe.multi()
            pipe.zadd(RELEASE_PENDING_KEY, {key: score})
            return True
        return redis_client.transaction(fn, key, RELEASE_PENDING_KEY, value_from_callable=True)

    def release(self, rev: int) -> str:
        """해제와 영구 감사 영수증을 한 Redis 트랜잭션으로 저장한다."""
        receipt_key = self.release_key(rev)
        def fn(pipe):
            # 이전에 승인된 동일 해제의 재처리. 새 주문 기록에는 손대지 않는다.
            if pipe.exists(receipt_key):
                return "ok"
            if pipe.exists(self.lock_key):
                return "주문이 진행 중입니다. 잠시 후 다시 눌러 주세요."
            rec = self._rec(pipe)
            if rec is None:
                return "이미 해제됐습니다."
            if rec.get("rev") != rev:
                return "새로운 확인 요청이 있습니다. 최신 알림의 버튼을 눌러 주세요."
            receipt = {
                "id": "cr" + hashlib.sha256(receipt_key.encode()).hexdigest()[:23],
                "monitorId": self._m, "supplier": self._s, "revision": rev,
                "accountKey": self.key,
                "releasedAt": datetime.now(timezone.utc).isoformat(),
            }
            pipe.multi()
            pipe.set(receipt_key, json.dumps(receipt, ensure_ascii=False))
            pipe.zadd(RELEASE_PENDING_KEY, {receipt_key: time.time()})
            pipe.delete(self.key, self.failed_key, self.reissue_key)
            return "ok"
        return self._tx(fn, self.lock_key, self.key, self.failed_key, self.reissue_key, receipt_key)


class CartGuardMixin:
    """정확 일치 규칙. 흐름: _cart_start → (변경은 모두 _cart_build) → _cart_finish."""
    cart_snapshot = None
    _cart_frozen = False      # 다름을 한 번이라도 봤으면 True — 이후 조작 금지
    _cart_touched = False     # 이 실행이 장바구니를 한 번이라도 바꿨는지

    def _cart_check(self, state: dict):
        """조작 직전 확인: 잠금 소유 + 장바구니 == state. 다르면 고정하고 예외."""
        if self._cart_frozen:
            raise CartChanged("이미 변경이 감지된 실행")
        try:
            if self.cart_snapshot is not None and not self.cart_snapshot.owned():
                raise CartChanged("실행 잠금을 잃음 — 장바구니를 더 조작하지 않음")
            now = self._cart_map()
        except Exception:
            self._cart_frozen = True
            raise
        if now != state:
            self._cart_frozen = True
            raise CartChanged(f"장바구니가 예상과 다름(예상 {state} / 현재 {now})")

    def _cart_build(self, current: dict, target: dict):
        """장바구니를 current → target 으로 바꾼다. 비우기 전·후, 품목 하나 담기 전·후마다 정확 대조."""
        if self._cart_frozen:
            raise CartChanged("이미 변경이 감지된 실행")
        self._cart_check(current)
        if current and hasattr(self, "_cart_delete_raw"):
            left = dict(current)                       # 품목 하나씩 삭제 — 삭제마다 전·후 대조(7차 R1)
            for k in list(current):
                self._cart_check(left)
                self._cart_touched = True
                self._cart_delete_raw(k)
                del left[k]
                self._cart_check(left)
        elif current:
            self._cart_touched = True
            self._cart_clear_raw()                     # 전체 비우기가 요청 1건인 도매(티제이팜)
            self._cart_check({})
        acc = {}
        for k, q in target.items():
            self._cart_check(acc)
            self._cart_touched = True
            self._cart_add_raw(k, q)
            acc = {**acc, k: acc.get(k, 0) + q}
            self._cart_check(acc)

    def _cart_start(self):
        """(오류 문구 | None, 원래 장바구니). 오류면 장바구니를 건드리지 않았고 잠금도 없다."""
        self._cart_frozen = self._cart_touched = False
        store = self.cart_snapshot
        if store is None:
            return None, self._cart_map()
        if not store.lock():
            return "다른 주문이 이 도매 장바구니를 사용 중", {}
        try:
            now = self._cart_map()
            rec = store.load()
            if rec is not None:
                if now != store.snap_of(rec):
                    store.reissue(rec)
                    store.unlock()
                    return "이전 주문의 장바구니 확인이 끝나지 않음 — 확인 완료 버튼을 눌러 주세요", now
                store.clear_stale(rec["rev"])
            store.save(now)
            return None, now
        except Exception:
            store.unlock()
            raise

    def _cart_finish(self, snap: dict, allowed_states):
        """finally 에서 정확히 1회. 손대기 전이면 그냥 끝, 변경 감지면 확인 요청, 아니면 원래대로 복원."""
        store = self.cart_snapshot
        try:
            if not self._cart_touched:
                if store:
                    store.done()
                return True
            if not self._cart_frozen:
                now = self._cart_map()
                if now == snap:
                    if store:
                        store.done()
                    return True
                if any(now == s for s in allowed_states):
                    self._cart_build(now, snap)
                    if store:
                        store.done()
                    return True
                self._cart_frozen = True
            if store:
                store.restore_failed("주문 중 장바구니가 바뀌어 원래대로 되돌리지 않음")
            return False
        except Exception as e:
            logger.error("[%s] 장바구니 복원 중단: %s", getattr(self, "SUPPLIER_NAME", "?"), e)
            if store:
                try:
                    store.restore_failed(f"장바구니 복원 중단: {e}")
                except Exception as e2:
                    logger.error("실패 표시 기록 실패: %s", e2)
            return False
        finally:
            if store:
                try:
                    store.unlock()
                except Exception as e:
                    logger.error("장바구니 잠금 해제 실패(만료로 풀림): %s", e)
