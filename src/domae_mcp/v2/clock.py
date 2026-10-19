"""도매 v2 시각과 오프라인 lease (스펙 B5 실행 중 집행, P8). 시각 역행 허용 오차는 두지 않는다.

관측 최고 시각(time_high_water)에는 로컬 wall 관측만 쓴다(2026-10-19 사용자 결정 A, 계획 P8 수정).
서버 issued_at 은 그 번들의 cache 모드 판정(now < issued_at → ClockUntrusted)에만 쓴다. 서버 시계가 로컬보다
빠른 정상 시차에서 실행 중 차단·버전 확인 반복·불필요 POST 가 생기지 않게 하기 위해서다.
"""
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

# 관측 최고 시각 저장 간격(초). 계획의 10분은 "저장 → 사용 → 종료 → 시계를 저장 시각으로 되돌려 재시작" 으로
# 잔량을 되살릴 수 있어(Codex 전체 검수 1차) 진입 때마다(초 단위로 바뀔 때만) 저장한다.
HIGH_WATER_PERSIST_S = 1
# 단조 시간으로 계산한 시각 하한에만 두는 허용(사용자 결정 A, 2026-10-19). 단조 시계가 wall 보다 미세하게 빠를 때
# 오프라인 장기 실행의 오탐을 막는다. wall 기준 관측 최고 시각 검사는 허용 0 이다.
MONO_TOLERANCE_S = 2.0
MONO_TOLERANCE_RATE = 0.001


class ClockUntrusted(Exception):
    """로컬 시각이 관측 최고 시각이나 발급 시각보다 이르다. 온라인 확인 전에는 실행하지 않는다."""


@dataclass
class Clock:
    wall: Callable[[], int] = field(default=lambda: int(time.time()))
    mono: Callable[[], float] = field(default=time.monotonic)


def effective_now_network(issued_at: int, now: int) -> int:
    return max(issued_at, now)


def effective_now_cache(now: int, high_water: int, issued_at: int) -> int:
    if now < high_water or now < issued_at:
        raise ClockUntrusted()
    return max(now, high_water)


def next_high_water(stored: int, now: int) -> int:
    """로컬 wall 관측만 반영한다(결정 A)."""
    return max(stored, now)


class Lease:
    """남은 오프라인 실행 시간. 진입점마다 check() 로 줄기만 한다(늘어나는 경로가 없다)."""

    def __init__(self, offline_deadline: int, expires_at: int, effective_now: int, clock: Clock):
        self.offline_deadline = offline_deadline
        self.expires_at = expires_at
        self._clock = clock
        self._lock = threading.Lock()
        self._remaining = max(0, offline_deadline - effective_now)
        self._last_mono = clock.mono()
        self._last_wall = clock.wall()

    @property
    def remaining(self) -> int:
        return int(self._remaining)

    def check(self) -> int:
        with self._lock:
            mono, wall = self._clock.mono(), self._clock.wall()
            spent = max(mono - self._last_mono, wall - self._last_wall, 0)
            self._remaining = max(0, min(self._remaining - spent, self.offline_deadline - wall))
            self._last_mono, self._last_wall = mono, wall
            return int(self._remaining)

    def needs_renewal(self) -> bool:
        return self._clock.wall() >= self.expires_at

    def wall_regressed(self, high_water: int) -> bool:
        return self._clock.wall() < high_water

    def cap(self, other_remaining: int) -> None:
        """같은 발급본(request_id·issued_at 동일) 재설치일 때만 부른다. 기존 잔량보다 늘지 않게 한다."""
        with self._lock:
            self._remaining = max(0, min(self._remaining, other_remaining))
