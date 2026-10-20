"""도매 v2 진입점 집행·갱신 (스펙 B5 실행 중 집행·모든 진입점, B9 backoff, P8~P11·P13·P15·P20).

RuntimeGate 는 활성 컨텍스트 (key_fp, context_seq) 를 갖는다. 갱신·전환 작업은 시작할 때 컨텍스트를 받아 두고,
상태·캐시·실행 교체 직전에 게이트 잠금 안에서 다시 확인한다. 다르면 결과를 버린다.
크롤러 실행(load_modules)·키 커밋·레지스트리 교체는 publish() 가 잠금 하나 안에서 한다.
네트워크를 쓰는 흐름(시작·갱신·재검증·전환)은 동기화 잠금 하나로 직렬화한다(POST 중복 방지).
"""
import logging
import random
import threading
from dataclasses import dataclass
from typing import Callable

from domae_mcp.core.crawlers.registry import CrawlerBlocked, CrawlerRegistry

from .artifacts import LoadFailed, load_modules, unload_all
from .client import next_backoff
from .clock import HIGH_WATER_PERSIST_S, MONO_TOLERANCE_RATE, MONO_TOLERANCE_S, Clock, Lease, next_high_water
from .encoding import key_fp_from_bearer
from .keyslot import KeySlots
from .loader import Prepared, V2Error, V2Loader, V2Revoked
from .store import KeyState, StateStore, StoreError
from .verify import MinPolicy, PayloadRejected, check_loaded
from .winprotect import Protector

logger = logging.getLogger(__name__)

TERMINAL = ("key_revoked", "device_revoked", "protocol_too_old")
RECOVERABLE = ("clock_untrusted", "offline_deadline", "not_loaded", "downgrade")
_NEEDS_DOWNLOAD = ("offline_deadline", "not_loaded", "downgrade")

__all__ = ["RuntimeGate", "CrawlerBlocked", "TERMINAL", "RECOVERABLE"]


def _thread_runner(fn: Callable[[], None]) -> threading.Thread:
    t = threading.Thread(target=fn, name="domae-v2-refresh", daemon=True)
    t.start()
    return t


def _revoked_reason(ks: KeyState) -> str:
    return "key_revoked" if ks.revoked["scope"] == "key" else "device_revoked"


def _cancelled(e: V2Error) -> bool:
    return e.reason == "cancelled"


@dataclass
class _Running:
    prepared: Prepared
    lease: Lease


class RuntimeGate:
    def __init__(self, slots: KeySlots, state: StateStore, prot: Protector, clock: Clock | None = None,
                 loader_factory: Callable[..., V2Loader] | None = None, registry=CrawlerRegistry,
                 runner: Callable[[Callable[[], None]], object] = _thread_runner, loader_kw: dict | None = None):
        self._slots = slots
        self._state = state
        self._prot = prot
        self._clock = clock or Clock()
        self._registry = registry
        self._runner = runner
        self._loader_kw = loader_kw or {}
        self._loader_factory = loader_factory or self._default_loader
        self._lock = threading.RLock()          # 상태·실행 교체
        self._sync_lock = threading.Lock()      # 네트워크 흐름 직렬화(잠금 순서: _sync_lock → _lock)
        self._key_fp: bytes | None = None       # 실행 중(활성) 키
        self._bearer: str | None = None
        self._ctx_key: bytes | None = None      # 현재 컨텍스트의 대상 키(전환 중이면 후보)
        self._ctx_seq = 0
        self._running: _Running | None = None
        self._block: str | None = None
        self._version_failed = False
        self._last_error: str | None = None
        self._worker_loader: V2Loader | None = None
        self._worker = None
        self._version_retry_at = 0
        self._version_level = 0
        self._last_hw_persist = 0
        # 관측 최고 시각 추적 (hw, 그때의 monotonic). 다음 진입까지 흐른 단조 시간만큼 hw 를 밀어 올려
        # "사용 → 다음 진입 전에 wall 을 되돌림 → 재시작" 으로 잔량을 되살리지 못하게 한다(Codex 2차)
        self._hw_track: tuple[float, float] | None = None   # (기준 시각, 그때의 monotonic) — 진입마다 옮기지 않는다

    # ── 조립 ──

    def _default_loader(self, bearer: str, still_current: Callable[[], bool],
                        policy_write: Callable[[bytes, Callable[[KeyState], KeyState]], KeyState]) -> V2Loader:
        fp = key_fp_from_bearer(bearer)
        return V2Loader(bearer, self._slots.cache_dir(fp), self._state, self._prot, clock=self._clock,
                        still_current=still_current, policy_write=policy_write, **self._loader_kw)

    def _loader(self, bearer: str, ctx: tuple) -> V2Loader:
        return self._loader_factory(bearer, lambda: self.context() == ctx, self._policy_writer(ctx))

    def install_guard(self) -> None:
        self._registry.set_guard(self.require)

    def _key_state(self, key_fp: bytes) -> KeyState:
        try:
            return self._state.get(key_fp)
        except StoreError:  # 손상 파일은 격리되고 다음 읽기부터 빈 상태다
            return self._state.get(key_fp)

    # ── 컨텍스트 ──

    def context(self) -> tuple:
        with self._lock:
            return (self._ctx_key, self._ctx_seq)

    def switch_key(self, key_fp: bytes) -> tuple:
        """키 전환 시작. 진행 중 갱신을 취소하고 새 컨텍스트를 돌려준다. 활성 키는 publish 커밋 전까지 그대로다."""
        with self._lock:
            self._ctx_seq += 1
            self._ctx_key = key_fp
            self._cancel_worker()
            return (self._ctx_key, self._ctx_seq)

    def abort_switch(self, ctx: tuple) -> None:
        with self._lock:
            if self.context() == ctx:
                self._ctx_seq += 1
                self._ctx_key = self._key_fp

    def _cancel_worker(self) -> None:
        ld = self._worker_loader
        if ld is not None:
            ld.close()
        self._worker_loader = None

    # ── 보안 정책 쓰기(잠금 안, 실행 중 번들과 비교해 즉시 차단, 4차 M1) ──

    def _policy_writer(self, ctx: tuple):
        def write(key_fp: bytes, fn: Callable[[KeyState], KeyState]) -> KeyState:
            with self._lock:
                ks = self._state.update(key_fp, fn, create=self.context() == ctx)
                self._enforce_policy(key_fp, ks)
                return ks
        return write

    def _enforce_policy(self, key_fp: bytes, ks: KeyState) -> None:
        """실행 중(활성) 키의 저장 상태로 집행한다. 철회는 컨텍스트와 무관하게 종결 차단이다."""
        if key_fp != self._key_fp or self._block in TERMINAL:
            return
        if ks.revoked is not None:
            self._set_block(_revoked_reason(ks))
            return
        run = self._running
        if run is None:
            return
        p = run.prepared
        if ks.device_generation is not None and ks.device_generation > p.device_generation:
            self._set_block("downgrade")
            return
        try:
            check_loaded(MinPolicy(ks.min_release_seq, ks.min_protocol_version), p.release_seq)
        except PayloadRejected as e:
            self._set_block(e.reason)

    # ── 차단 ──

    def _set_block(self, reason: str) -> None:
        if self._block in TERMINAL:
            return
        self._block = reason
        if reason in TERMINAL:
            self._ctx_seq += 1
            self._cancel_worker()
            self._running = None
            self._registry.clear()
            unload_all()
        logger.warning("v2 크롤러 차단: %s", reason)

    def block(self, reason: str, ctx: tuple | None = None) -> bool:
        with self._lock:
            if ctx is not None and ctx != self.context():
                return False
            self._set_block(reason)
            return True

    def _block_revoked(self, e: V2Revoked, key_fp: bytes) -> None:
        """검증된 철회. 실행 중인 그 키면 컨텍스트와 무관하게 막는다(진행 중인 전환과 경합해도 새지 않게)."""
        with self._lock:
            if key_fp == self._key_fp:
                self._set_block(e.reason)

    # ── 실행 교체 ──

    def publish(self, p: Prepared, ctx: tuple, commit: Callable[[], None] | None = None,
                bearer: str | None = None) -> bool:
        """검사를 모두 끝낸 뒤에만 부수효과를 낸다. 거부면 False(exec 0회·포인터·sys.modules·레지스트리 불변)."""
        with self._lock:
            if self.context() != ctx or p.key_fp != ctx[0]:
                return False
            ks = self._key_state(p.key_fp)
            if ks.revoked is not None:
                return False
            run = self._running
            if run is not None and run.prepared.key_fp == p.key_fp:
                cur = run.prepared
                if p.release_seq < cur.release_seq or (p.release_seq == cur.release_seq
                                                       and p.issued_at < cur.issued_at):
                    return False  # 같은 키에서 더 오래된 발급본으로 되돌리지 않는다
            if ks.device_generation is not None and p.device_generation < ks.device_generation:
                if p.key_fp == self._key_fp:
                    self._set_block("downgrade")
                return False
            try:
                check_loaded(MinPolicy(ks.min_release_seq, ks.min_protocol_version), p.release_seq)
            except PayloadRejected as e:
                if p.key_fp == self._key_fp:
                    self._set_block(e.reason)
                return False
            classes, swap = load_modules(p.plain, p.manifest)  # LoadFailed 는 호출자가 backoff 로 처리
            switching = p.key_fp != self._key_fp
            if commit is not None:
                try:
                    commit()
                except BaseException:
                    swap.rollback()
                    raise
            now = self._clock.wall()
            same = run is not None and run.prepared.key_fp == p.key_fp and run.prepared.issuance == p.issuance
            eff = max(p.issued_at, now) if p.mode == "network" else max(now, ks.time_high_water, p.issued_at)
            lease = Lease(p.offline_deadline, p.expires_at, eff, self._clock)
            if same:
                lease.cap(run.lease.check())
            self._registry.register_all(classes)
            self._running = _Running(p, lease)
            self._last_error = None  # 로드 성공 — 이전 실패 사유(no_key·no_cache 등)는 더 이상 현재 상태가 아니다
            self._reset_hw_track(max(now, ks.time_high_water))
            if switching:
                self._key_fp = p.key_fp
                self._bearer = bearer or self._bearer
                self._ctx_key = p.key_fp
                self._ctx_seq += 1
            self._block = None
            swap.finalize()
            if p.mode == "network":
                try:
                    self._state.update(p.key_fp, self._reset_backoff, create=False)
                except StoreError as e:
                    logger.warning("v2 backoff 초기화 생략: %s", e.reason)
            return True

    @staticmethod
    def _reset_backoff(s: KeyState) -> KeyState:
        s.backoff_until, s.backoff_level = 0, 0
        return s

    # ── 시작·갱신 흐름 ──

    def start(self) -> None:
        """Task 6 "시작" 흐름. 네트워크를 기다리므로 서버는 별도 스레드에서 부른다."""
        with self._sync_lock:
            active = self._slots.active()
            with self._lock:
                if active is None:
                    self._key_fp = self._bearer = self._ctx_key = None
                    self._last_error = "no_key"
                else:
                    self._key_fp, self._bearer = active
                    self._ctx_key = self._key_fp
                    self._ctx_seq += 1
                    ctx = self.context()
            if active is not None:
                try:
                    self._sync_locked(ctx, use_cache=True)
                except Exception as e:  # 경로(key_fp)가 든 traceback 은 남기지 않는다
                    logger.warning("v2 시작 흐름 오류: %s", type(e).__name__)
            try:
                self._slots.cleanup()
            except OSError:
                logger.warning("v2 키 폴더 정리 실패")

    def _worker_sync(self, ctx: tuple) -> None:
        """백그라운드 갱신·복구 작업. 다른 흐름이 돌고 있으면 아무것도 하지 않는다."""
        if not self._sync_lock.acquire(blocking=False):
            return
        try:
            self._sync_locked(ctx, use_cache=False)
        except Exception as e:
            logger.warning("v2 갱신 작업 오류: %s", type(e).__name__)
        finally:
            self._sync_lock.release()

    def _sync_locked(self, ctx: tuple, use_cache: bool) -> None:
        """(캐시 로드) → 버전 확인 → 필요하면 다운로드·publish·캐시 쓰기. _sync_lock 을 잡은 채로 부른다."""
        with self._lock:
            bearer = self._bearer
            if bearer is None or ctx[0] is None or self.context() != ctx:
                return
            loader = self._loader(bearer, ctx)
            self._worker_loader = loader
        try:
            self._sync_with(loader, ctx, use_cache)
        finally:
            with self._lock:
                if self._worker_loader is loader:
                    self._worker_loader = None

    def _sync_with(self, loader: V2Loader, ctx: tuple, use_cache: bool) -> None:
        key_fp = ctx[0]
        if use_cache and not self._key_state(key_fp).revoked:
            self._try_cache(loader, ctx)
        try:
            v = loader.check_version()
        except V2Revoked as e:
            self._block_revoked(e, key_fp)
            return
        except V2Error as e:
            self._version_failure(e, ctx)
            return
        with self._lock:
            if self.context() != ctx:
                return
            self._version_failed = False
            self._last_error = None
            self._version_level = 0
            self._version_retry_at = 0
            self._reset_hw_track(self._clock.wall())  # 검증된 버전 확인이 hw 를 now 로 재설정했다
            if self._block == "clock_untrusted":
                self._recover_clock()
            retry_cache = self._block == "clock_untrusted" or self._running is None
        if retry_cache and not self._key_state(key_fp).revoked:
            self._try_cache(loader, ctx)  # 시계가 복구됐으면 캐시를 다시 본다
        with self._lock:
            if self._block == "clock_untrusted" and self._running is None:
                # 캐시 발급 시각보다 로컬 시각이 아직 이르다(서버가 빠른 시차, 버전 확인으로 ±10분 안임을 확인함).
                # 다운로드로 카운터를 쓰지 않고 잠시 뒤 다시 본다(결정 A)
                self._version_retry_at = self._clock.wall() + 60
                return
        with self._lock:
            run = self._running
            cached = run.prepared if run is not None and run.prepared.key_fp == key_fp else None
            need_block = self._block in RECOVERABLE
        try:
            need = need_block or loader.needs_download(v, cached)
        except V2Error as e:
            self._last_error = e.reason
            logger.warning("v2 게시 정보 불일치: %s", e.reason)
            return
        if need:
            self._download(loader, ctx)

    def _try_cache(self, loader: V2Loader, ctx: tuple) -> None:
        try:
            p = loader.prepare_cached()
        except V2Revoked as e:
            self._block_revoked(e, ctx[0])
            return
        except V2Error as e:
            if e.reason == "clock":
                with self._lock:
                    if self.context() == ctx and self._running is None:
                        self._block = "clock_untrusted"
            self._last_error = e.reason
            return
        except Exception as e:
            self._last_error = "store"
            logger.warning("v2 캐시 준비 오류: %s", type(e).__name__)
            return
        try:
            self.publish(p, ctx)
        except LoadFailed as e:
            self._last_error = "load"
            logger.warning("v2 캐시 로드 실패: %s", e.reason)

    def _download(self, loader: V2Loader, ctx: tuple) -> bool:
        try:
            p = loader.prepare_download()
        except V2Revoked as e:
            self._block_revoked(e, ctx[0])
            return False
        except V2Error as e:
            self._last_error = e.reason
            return False
        try:
            ok = self.publish(p, ctx)
        except LoadFailed as e:
            loader.fail_backoff()
            self._last_error = "load"
            logger.warning("v2 번들 로드 실패: %s", e.reason)
            return False
        except Exception:
            loader.fail_backoff()
            raise
        if not ok:
            if self.context() == ctx:
                loader.fail_backoff()
            return False
        try:
            loader.write_cache(p)
        except OSError:
            logger.warning("v2 캐시 쓰기 실패(다음 시작은 다운로드한다)")
        return True

    def _version_failure(self, e: V2Error, ctx: tuple) -> None:
        if _cancelled(e):
            return
        with self._lock:
            if self.context() != ctx:
                return
            self._version_failed = True
            self._last_error = e.reason
            if e.reason == "protocol_too_old":
                self._set_block("protocol_too_old")
                return
            now = self._clock.wall()
            self._version_retry_at, self._version_level = next_backoff(self._version_level, now, e.retry_after,
                                                                      random.random)

    def _recover_clock(self) -> None:
        """검증된 버전 확인 성공 뒤(잠금 안). 같은 번들이면 lease 를 network 시각으로 다시 계산해 작은 값으로 푼다."""
        run = self._running
        if run is None:
            return
        now = self._clock.wall()
        p = run.prepared
        lease = Lease(p.offline_deadline, p.expires_at, max(p.issued_at, now), self._clock)
        lease.cap(run.lease.check())
        self._running = _Running(p, lease)
        self._block = None
        self._reset_hw_track(now)  # 검증된 서버 시각으로 hw 를 재설정했다

    # ── 진입점 집행 ──

    def require(self) -> None:
        with self._lock:
            if self._block in TERMINAL:
                raise CrawlerBlocked(self._block)
            if self._block is not None:
                self._kick()
                raise CrawlerBlocked(self._block)
            run = self._running
            if run is None:
                self._kick()
                raise CrawlerBlocked("not_loaded")
            if run.lease.check() == 0:
                # 만료로 막을 때도 단조 하한을 저장한다(되돌린 시계로 재시작해 잔량을 되살리지 못하게, Codex 4차)
                self._persist_mono_floor(run.prepared.key_fp)
                self._set_block("offline_deadline")
                self._kick()
                raise CrawlerBlocked("offline_deadline")
            now = self._clock.wall()
            ks = self._key_state(run.prepared.key_fp)
            self._enforce_policy(run.prepared.key_fp, ks)
            if self._block is not None:
                if self._block not in TERMINAL:
                    self._kick()
                raise CrawlerBlocked(self._block)
            # ① wall 기준 관측 최고 시각(허용 0)  ② 기준점 뒤 흐른 단조 시간(float 누적, 허용 2초+경과의 0.1%)
            # — 사용자 결정 A(2026-10-19, Codex 3차). ②는 "사용 → 다음 진입 전 wall 되돌림 → 재시작" 을 막는다
            mono_floor = self._mono_floor(ks.time_high_water)
            if now < ks.time_high_water or now < mono_floor:
                # 단조 시간으로 계산한 하한을 저장해 재시작이 되돌린 시계로 잔량을 되살리지 못하게 한다
                floor = max(ks.time_high_water, mono_floor)
                try:
                    self._state.update(run.prepared.key_fp,
                                       lambda s: (setattr(s, "time_high_water", max(s.time_high_water, floor)), s)[1],
                                       create=False)
                except StoreError as e:
                    logger.warning("v2 시각 기록 생략: %s", e.reason)
                self._set_block("clock_untrusted")
                self._kick()
                raise CrawlerBlocked("clock_untrusted")
            hw = max(next_high_water(ks.time_high_water, now), mono_floor)
            if now - self._last_hw_persist >= HIGH_WATER_PERSIST_S or hw - ks.time_high_water >= HIGH_WATER_PERSIST_S:
                self._last_hw_persist = now
                try:
                    if hw != ks.time_high_water:
                        self._state.update(run.prepared.key_fp, lambda s: (setattr(s, "time_high_water", hw), s)[1],
                                           create=False)
                except StoreError as e:
                    logger.warning("v2 시각 기록 생략: %s", e.reason)
            if run.lease.needs_renewal() and ks.backoff_until <= now:
                self._kick()

    def _mono_floor(self, stored_hw: int) -> int:
        """기준점 뒤 흐른 단조 시간으로 계산한 시각 하한(허용 2초+경과의 0.1%, 결정 A). 잠금 안."""
        mono = self._clock.mono()
        if self._hw_track is None:
            self._hw_track = (float(stored_hw), mono)
        elapsed = max(0.0, mono - self._hw_track[1])
        return int(self._hw_track[0] + elapsed - (MONO_TOLERANCE_S + MONO_TOLERANCE_RATE * elapsed))

    def _persist_mono_floor(self, key_fp: bytes) -> None:
        try:
            ks = self._key_state(key_fp)
            floor = max(self._mono_floor(ks.time_high_water), self._clock.wall())
            if floor > ks.time_high_water:
                self._state.update(key_fp, lambda s: (setattr(s, "time_high_water", max(s.time_high_water, floor)), s)[1],
                                   create=False)
        except StoreError as e:
            logger.warning("v2 시각 기록 생략: %s", e.reason)

    def _reset_hw_track(self, base: int) -> None:
        """publish·검증된 버전 확인·시계 복구 때 단조 추적의 기준점을 지금으로 잡는다(잠금 안)."""
        self._hw_track = (float(base), self._clock.mono())

    def _kick(self) -> None:
        """복구·갱신 작업을 백그라운드로 1개만 시작한다(잠금 안). 네트워크를 기다리지 않는다."""
        if self._block in TERMINAL or self._bearer is None or self._key_fp is None:
            return
        if self._sync_lock.locked():  # 다른 흐름(시작·재검증·전환·갱신)이 돌고 있다
            return
        if self._worker is not None and getattr(self._worker, "is_alive", lambda: False)():
            return
        if self._ctx_key != self._key_fp:  # 키 전환 중에는 옛 키 갱신을 시작하지 않는다
            return
        now = self._clock.wall()
        if self._version_retry_at > now:
            return
        if self._block in _NEEDS_DOWNLOAD and self._key_state(self._key_fp).backoff_until > now:
            return  # 다운로드 backoff 중에는 버전 확인만 반복하지 않는다
        ctx = self.context()
        self._worker = self._runner(lambda: self._worker_sync(ctx))

    # ── 설정 화면용 흐름 ──

    def reverify(self) -> int:
        """같은 키 재검증: 활성 키 폴더의 로더로 버전 확인 → 필요할 때만 다운로드. 철회면 게이트를 막는다."""
        with self._lock:
            self._cancel_worker()
        with self._sync_lock:
            return self._reverify_locked()

    def _reverify_locked(self) -> int:
        with self._lock:
            bearer, ctx, key_fp = self._bearer, self.context(), self._key_fp
        if bearer is None or key_fp is None:
            raise V2Error("no_key")
        loader = self._loader(bearer, ctx)
        with self._lock:
            self._worker_loader = loader  # 종결 차단·전환이 이 요청도 취소할 수 있게(P20)
        try:
            return self._reverify_with(loader, ctx, key_fp)
        finally:
            with self._lock:
                if self._worker_loader is loader:
                    self._worker_loader = None

    def _reverify_with(self, loader: V2Loader, ctx: tuple, key_fp: bytes) -> int:
        try:
            v = loader.check_version()
        except V2Revoked as e:
            self._block_revoked(e, key_fp)
            raise
        except V2Error as e:
            if e.reason == "protocol_too_old":
                self.block("protocol_too_old", ctx)
            raise
        with self._lock:
            self._last_error = None
            self._reset_hw_track(self._clock.wall())
            if self._block == "clock_untrusted":
                self._recover_clock()
            run = self._running
            cached = run.prepared if run is not None and run.prepared.key_fp == key_fp else None
            blocked = self._block in RECOVERABLE
        if blocked or loader.needs_download(v, cached):
            try:
                p = loader.prepare_download()
            except V2Revoked as e:
                self._block_revoked(e, key_fp)
                raise
            try:
                ok = self.publish(p, ctx)
            except LoadFailed as e:
                loader.fail_backoff()
                raise V2Error("load", detail=e.reason) from None
            if not ok:
                loader.fail_backoff()
                raise V2Error("rejected")
            loader.write_cache(p)
        return self._registry.count()

    def switch(self, bearer: str) -> int:
        """다른 키로 전환. 전체 로드가 성공한 뒤에만 포인터를 바꾼다. 실패하면 활성 키·캐시·게이트 그대로."""
        fp = key_fp_from_bearer(bearer)
        with self._lock:
            self._cancel_worker()
        with self._sync_lock:
            with self._lock:
                same = fp == self._key_fp
                if not same:
                    ctx = self.switch_key(fp)
            if same:
                return self._reverify_locked()
            try:
                self._slots.stage(bearer)
                loader = self._loader(bearer, ctx)
                loader.check_version()
                p = loader.prepare_download()
                if not loader.write_cache(p):
                    raise V2Error("stale")
                try:
                    ok = self.publish(p, ctx, commit=lambda: self._slots.commit(fp), bearer=bearer)
                except LoadFailed as e:
                    loader.fail_backoff()
                    raise V2Error("load", detail=e.reason) from None
                if not ok:
                    raise V2Error("rejected")
            except BaseException:
                self.abort_switch(ctx)
                raise
            try:
                self._state.forget_others(fp)  # 이전 키와 실패한 후보 키의 상태를 지운다(P7 키 변경)
            except StoreError:
                pass
            try:
                self._slots.cleanup()
            except OSError:
                pass
            return self._registry.count()

    # ── 상태 ──

    def status(self) -> dict:
        with self._lock:
            run = self._running
            if self._key_fp is None:
                state = "no_key"
            elif self._block is not None:
                state = "blocked"
            elif run is None:
                state = "not_loaded"
            else:
                state = "ready"
            return {
                "state": state,
                "reason": self._block,
                "release_seq": run.prepared.release_seq if run else None,
                "expires_at": run.prepared.expires_at if run else None,
                "offline_deadline": run.prepared.offline_deadline if run else None,
                "remaining_s": run.lease.remaining if run else 0,
                "crawler_count": self._registry.count() if run else 0,
                "version_check_failed": self._version_failed,
                "last_error": self._last_error,
                "protected": bool(getattr(self._prot, "protected", False)),
            }
