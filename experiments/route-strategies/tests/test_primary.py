import pytest

from capacity_tracker import CapacityTracker
from conftest import FakeClock
from routing import Primary, pickers_for

HOME, REMOTE, THIRD = "australiaeast", "eastus", "westus3"
ORDER = (HOME, REMOTE)
CAPACITY = 6000.0


def make(order=ORDER, clock=None):
    clock = clock or FakeClock()
    tracker = CapacityTracker({region: CAPACITY for region in order}, clock=clock)
    primary = Primary(order)
    return tracker, primary, clock


def test_primary_starts_at_the_first_region():
    _, primary, _ = make()

    assert primary.current == HOME


def test_primary_rejects_unknown_region():
    _, primary, _ = make()

    with pytest.raises(ValueError):
        primary.switch_to("nowhere")


@pytest.mark.parametrize("strategy", ["429-spillover", "capacity-aware"])
def test_pick_stays_on_primary_while_it_is_usable(strategy):
    tracker, primary, _ = make()
    pick = pickers_for(primary)[strategy]

    for _ in range(3):
        assert pick(tracker, ORDER, 100).region == HOME
    assert primary.current == HOME


def test_spillover_switches_primary_after_429_and_stays_there():
    tracker, primary, clock = make()
    pick = pickers_for(primary)["429-spillover"]
    tracker.mark_throttled(HOME, 2.0)

    first = pick(tracker, ORDER, 100)

    assert (first.region, primary.current) == (REMOTE, REMOTE)
    # 원래 리전이 풀려도 primary 는 그대로 유지한다
    clock.advance(2.0)
    assert pick(tracker, ORDER, 100).region == REMOTE


def test_spillover_rotates_back_when_the_new_primary_is_throttled():
    tracker, primary, _ = make()
    pick = pickers_for(primary)["429-spillover"]
    tracker.mark_throttled(HOME, 2.0)
    pick(tracker, ORDER, 100)          # primary -> eastus
    tracker.mark_throttled(REMOTE, 2.0)
    tracker.mark_unavailable(HOME, 0)  # 호주는 풀렸다

    assert pick(tracker, ORDER, 100).region == HOME
    assert primary.current == HOME


def test_spillover_waits_for_the_region_that_unblocks_first_and_makes_it_primary():
    tracker, primary, _ = make()
    pick = pickers_for(primary)["429-spillover"]
    tracker.mark_throttled(HOME, 5.0)
    tracker.mark_throttled(REMOTE, 2.0)

    choice = pick(tracker, ORDER, 100)

    assert (choice.region, choice.wait) == (REMOTE, pytest.approx(2.0))
    assert primary.current == REMOTE


def test_capacity_aware_switches_primary_before_the_bucket_overflows():
    tracker, primary, _ = make()
    pick = pickers_for(primary)["capacity-aware"]
    tracker.reserve(HOME, 5500)

    choice = pick(tracker, ORDER, 1000)

    assert (choice.region, choice.wait, primary.current) == (REMOTE, 0, REMOTE)


def test_capacity_aware_keeps_new_primary_even_when_the_old_one_drains():
    tracker, primary, clock = make()
    pick = pickers_for(primary)["capacity-aware"]
    tracker.reserve(HOME, 5500)
    pick(tracker, ORDER, 1000)  # primary -> eastus

    clock.advance(60)  # 호주 버킷이 다 빠졌다

    assert pick(tracker, ORDER, 1000).region == REMOTE


def test_capacity_aware_waits_for_the_earliest_room_when_every_region_is_full():
    tracker, primary, _ = make()
    pick = pickers_for(primary)["capacity-aware"]
    tracker.reserve(HOME, 5800)   # 800 초과 -> 8초
    tracker.reserve(REMOTE, 5500)  # 500 초과 -> 5초

    choice = pick(tracker, ORDER, 1000)

    assert (choice.region, choice.wait, primary.current) == (REMOTE, pytest.approx(5.0), REMOTE)


def test_rotation_follows_the_configured_order_from_the_current_primary():
    order = (HOME, REMOTE, THIRD)
    tracker, primary, _ = make(order)
    pick = pickers_for(primary)["429-spillover"]
    tracker.mark_throttled(HOME, 5.0)
    tracker.mark_throttled(REMOTE, 5.0)

    assert pick(tracker, order, 100).region == THIRD
    assert primary.current == THIRD
