"""도매 v2 시각·lease 시험 (계획 Task 3 표). 가짜 시계만 쓴다."""
import pytest

from domae_mcp.v2.clock import (Clock, ClockUntrusted, Lease, effective_now_cache, effective_now_network,
                                next_high_water)

T0 = 1_800_000_000
H = 3600


class FakeTime:
    def __init__(self, wall: int, mono: float = 1000.0):
        self.w, self.m = wall, mono

    def clock(self) -> Clock:
        return Clock(wall=lambda: self.w, mono=lambda: self.m)

    def advance(self, wall: int = 0, mono: float = 0):
        self.w += wall
        self.m += mono


def _cache_lease(t: FakeTime, issued=T0, high_water=0):
    eff = effective_now_cache(t.w, high_water, issued)
    return Lease(issued + 144 * H, issued + 72 * H, eff, t.clock())


def test_cache_reload_120h_after_issue():
    t = FakeTime(T0 + 120 * H)
    lease = _cache_lease(t, high_water=T0 + 119 * H)
    assert lease.check() == 24 * H


@pytest.mark.parametrize("now,positive", [(T0 + 11 * 60, True), (T0 + 72 * H + 1, True), (T0 + 144 * H, False)])
def test_cache_11min_72h_ok_144h_zero(now, positive):
    lease = _cache_lease(FakeTime(now))
    assert (lease.check() > 0) is positive


def test_wall_regression_untrusted():
    with pytest.raises(ClockUntrusted):
        effective_now_cache(T0 + 10 * H - 1, T0 + 10 * H, T0)


def test_now_before_issued_untrusted():
    with pytest.raises(ClockUntrusted):
        effective_now_cache(T0 - 1, 0, T0)


def test_high_water_takes_local_now_only():
    assert next_high_water(T0, T0 + 50) == T0 + 50
    assert next_high_water(T0 + 100, T0 + 50) == T0 + 100


def test_network_effective_now():
    assert effective_now_network(T0, T0 - 30) == T0
    assert effective_now_network(T0, T0 + 30) == T0 + 30


def test_same_issuance_reinstall_keeps_smaller():
    t = FakeTime(T0)
    first = Lease(T0 + 144 * H, T0 + 72 * H, effective_now_network(T0, t.w), t.clock())
    t.advance(wall=3 * H, mono=3 * H)
    before = first.check()
    t.advance(wall=-60)
    again = Lease(T0 + 144 * H, T0 + 72 * H, effective_now_network(T0, t.w), t.clock())
    assert again.remaining > before  # cap 없이는 늘어난다
    again.cap(before)
    assert again.remaining <= before


def test_new_issuance_same_release_gets_new_lease():
    t = FakeTime(T0)
    first = Lease(T0 + 144 * H, T0 + 72 * H, T0, t.clock())
    t.advance(wall=72 * H, mono=72 * H)
    issued2 = t.w
    second = Lease(issued2 + 144 * H, issued2 + 72 * H, effective_now_network(issued2, t.w), t.clock())
    t.advance(wall=78 * H, mono=78 * H)  # T0+150H
    assert first.check() == 0
    assert second.check() == 66 * H


def test_running_backstep_does_not_extend():
    t = FakeTime(T0)
    lease = Lease(T0 + 144 * H, T0 + 72 * H, T0, t.clock())
    start = lease.check()
    t.advance(wall=-7200, mono=3600)
    after_back = lease.check()
    assert start - after_back >= 3600
    assert lease.wall_regressed(T0)
    t.advance(wall=7200, mono=10)  # 원래 시각으로 다시 전진해도 늘지 않는다
    assert lease.check() <= after_back


def test_sleep_counts_via_wall():
    t = FakeTime(T0)
    lease = Lease(T0 + 144 * H, T0 + 72 * H, T0, t.clock())
    start = lease.check()
    t.advance(wall=5 * H, mono=10)
    assert start - lease.check() >= 5 * H


def test_forward_jump_expires():
    t = FakeTime(T0)
    lease = Lease(T0 + 144 * H, T0 + 72 * H, T0, t.clock())
    t.advance(wall=200 * H)
    assert lease.check() == 0
    t.advance(wall=-200 * H)
    assert lease.check() == 0  # 되돌려도 늘지 않는다


def test_needs_renewal_at_expires():
    t = FakeTime(T0 + 72 * H - 1)
    lease = Lease(T0 + 144 * H, T0 + 72 * H, T0, t.clock())
    assert lease.needs_renewal() is False
    t.advance(wall=1)
    assert lease.needs_renewal() is True
