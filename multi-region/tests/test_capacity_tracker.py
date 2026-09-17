import pytest

from capacity_tracker import CapacityTracker
from conftest import FakeClock

# 1분에 6,000 토큰 = 초당 100 토큰씩 빠진다.
CAPACITY = 6000.0


def make_tracker(headroom=1.0):
    clock = FakeClock()
    tracker = CapacityTracker({"eastus2": CAPACITY, "australiaeast": CAPACITY},
                              headroom=headroom, clock=clock)
    return tracker, clock


def test_new_region_is_empty_and_has_room():
    tracker, _ = make_tracker()

    assert tracker.utilization("eastus2") == 0
    assert tracker.has_room("eastus2", 1000)


def test_reserve_raises_utilization_and_drains_over_time():
    tracker, clock = make_tracker()

    tracker.reserve("eastus2", 3000)
    before = tracker.utilization("eastus2")
    clock.advance(10)  # 1,000 토큰 빠짐
    after = tracker.utilization("eastus2")

    assert before == pytest.approx(0.5)
    assert after == pytest.approx(2000 / CAPACITY)


def test_level_never_drains_below_zero():
    tracker, clock = make_tracker()
    tracker.reserve("eastus2", 100)

    clock.advance(3600)

    assert tracker.utilization("eastus2") == 0


def test_no_room_when_reservation_would_overflow():
    tracker, _ = make_tracker()
    tracker.reserve("eastus2", 5500)

    assert not tracker.has_room("eastus2", 600)
    assert tracker.has_room("eastus2", 500)


def test_headroom_lowers_the_limit():
    tracker, _ = make_tracker(headroom=0.5)
    tracker.reserve("eastus2", 2000)

    assert not tracker.has_room("eastus2", 1500)


def test_request_larger_than_capacity_is_allowed_on_empty_bucket():
    # 서비스는 현재 사용률만 본다. 비어 있으면 큰 요청도 받는다.
    tracker, _ = make_tracker()

    assert tracker.has_room("eastus2", CAPACITY * 3)


def test_settle_corrects_estimate_to_actual():
    tracker, _ = make_tracker()
    tracker.reserve("eastus2", 3000)

    tracker.settle("eastus2", estimate=3000, actual=1200)

    assert tracker.utilization("eastus2") == pytest.approx(0.2)


def test_release_returns_the_whole_reservation():
    tracker, _ = make_tracker()
    tracker.reserve("eastus2", 3000)

    tracker.release("eastus2", 3000)

    assert tracker.utilization("eastus2") == 0


def test_throttled_region_is_blocked_and_saturated_until_retry_after():
    tracker, clock = make_tracker()

    tracker.mark_throttled("eastus2", 2.0)
    blocked = (tracker.blocked_for("eastus2"), tracker.has_room("eastus2", 1))
    clock.advance(2.0)

    assert blocked == (pytest.approx(2.0), False)
    assert tracker.blocked_for("eastus2") == 0
    assert tracker.utilization("eastus2") == pytest.approx((CAPACITY - 200) / CAPACITY)


def test_unavailable_region_is_blocked_without_touching_level():
    tracker, _ = make_tracker()

    tracker.mark_unavailable("eastus2", 5.0)

    assert tracker.blocked_for("eastus2") == pytest.approx(5.0)
    assert tracker.utilization("eastus2") == 0


def test_seconds_until_room_waits_for_overflow_to_drain():
    tracker, _ = make_tracker()
    tracker.reserve("eastus2", 5500)

    # 5,500 + 1,000 - 6,000 = 500 토큰 초과 -> 5초
    assert tracker.seconds_until_room("eastus2", 1000) == pytest.approx(5.0)


def test_seconds_until_room_never_exceeds_time_to_empty():
    tracker, _ = make_tracker()
    tracker.reserve("eastus2", 300)

    # 버킷보다 큰 요청이라도 비는 순간(3초) 받아준다.
    assert tracker.seconds_until_room("eastus2", CAPACITY * 3) == pytest.approx(3.0)


def test_seconds_until_room_respects_block():
    tracker, _ = make_tracker()
    tracker.mark_unavailable("eastus2", 7.0)

    assert tracker.seconds_until_room("eastus2", 1) == pytest.approx(7.0)


def test_unknown_region_raises():
    tracker, _ = make_tracker()

    with pytest.raises(KeyError):
        tracker.utilization("westeurope")


def test_invalid_headroom_raises():
    with pytest.raises(ValueError):
        CapacityTracker({"eastus2": CAPACITY}, headroom=0)
