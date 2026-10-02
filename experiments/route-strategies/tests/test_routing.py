import threading
import time

import pytest

from capacity_tracker import CapacityTracker
from conftest import FakeClock
from routing import (
    MAX_WAIT_SECONDS,
    RECHECK_SECONDS,
    CallResult,
    Primary,
    pickers_for,
    retry_after_seconds,
    route_request,
)


def pickers(order=None):
    """primary 를 새로 만든 picker 한 쌍. 테스트마다 primary 상태를 나눈다."""
    return pickers_for(Primary(order or ORDER))


def spillover(order=None):
    return pickers(order)["429-spillover"]


def capacity(order=None):
    return pickers(order)["capacity-aware"]

HOME, REMOTE = "australiaeast", "eastus2"
ORDER = (HOME, REMOTE)
CAPACITY = 6000.0
OK_USAGE = {"prompt_tokens": 100, "completion_tokens": 0}


class FixedEstimator:
    """추정치를 고정하고 실제 비용은 prompt_tokens 를 그대로 쓰는 가짜."""

    def __init__(self, value):
        self.value = value
        self.observed = []

    def estimate(self):
        return self.value

    def actual_cost(self, usage):
        return float(usage["prompt_tokens"])

    def observe(self, cost):
        self.observed.append(cost)


class ScriptedSender:
    """리전별로 준비된 응답을 차례로 돌려주고 호출 순서를 기록한다."""

    def __init__(self, script):
        self.script = {region: list(results) for region, results in script.items()}
        self.calls = []

    def __call__(self, region):
        self.calls.append(region)
        return self.script[region].pop(0)


def ok():
    return CallResult(status=200, headers={"x-ms-region": "Australia East"}, usage=OK_USAGE)


def throttled(retry_after_ms="2000"):
    return CallResult(status=429, headers={"retry-after-ms": retry_after_ms}, usage=None)


def make(clock=None):
    clock = clock or FakeClock()
    tracker = CapacityTracker({HOME: CAPACITY, REMOTE: CAPACITY}, clock=clock)
    return tracker, clock


def run(sender, tracker, pick, estimator=None, sleeps=None, max_attempts=3, clock=None):
    sleeps = [] if sleeps is None else sleeps

    def sleep(seconds):
        sleeps.append(seconds)
        if clock is not None:
            clock.advance(seconds)

    return route_request(sender, ORDER, tracker, estimator or FixedEstimator(100), pick,
                         max_attempts=max_attempts, sleep=sleep)


# --- retry-after ---------------------------------------------------------------

@pytest.mark.parametrize("headers, expected", [
    ({"retry-after-ms": "1500", "retry-after": "9"}, 1.5),
    ({"Retry-After": "3"}, 3.0),
    ({"retry-after": "Wed, 21 Oct 2015 07:28:00 GMT"}, None),
    ({}, None),
])
def test_retry_after_prefers_milliseconds(headers, expected):
    assert retry_after_seconds(headers) == expected


# --- route_request -------------------------------------------------------------

def test_success_on_home_settles_estimate_to_actual():
    tracker, _ = make()
    estimator = FixedEstimator(3000)
    sender = ScriptedSender({HOME: [ok()]})

    outcome = run(sender, tracker, capacity(), estimator=estimator)

    assert (outcome.succeeded, outcome.region, outcome.attempts) == (True, HOME, 1)
    assert tracker.utilization(HOME) == pytest.approx(100 / CAPACITY)
    assert estimator.observed == [100.0]


def test_429_on_home_spills_to_remote_without_waiting():
    tracker, _ = make()
    sender = ScriptedSender({HOME: [throttled()], REMOTE: [ok()]})
    sleeps = []

    outcome = run(sender, tracker, spillover(), sleeps=sleeps)

    assert sender.calls == [HOME, REMOTE]
    assert (outcome.succeeded, outcome.region, outcome.attempts) == (True, REMOTE, 2)
    assert sleeps == []
    assert tracker.blocked_for(HOME) == pytest.approx(2.0)


def test_429_without_retry_after_uses_default_block():
    tracker, _ = make()
    sender = ScriptedSender({HOME: [CallResult(429, {}, None)], REMOTE: [ok()]})

    run(sender, tracker, spillover())

    assert tracker.blocked_for(HOME) > 0


def test_all_regions_throttled_waits_then_retries():
    tracker, clock = make()
    sender = ScriptedSender({
        HOME: [throttled("3000")],
        REMOTE: [throttled("1000"), ok()],
    })
    sleeps = []

    outcome = run(sender, tracker, spillover(), sleeps=sleeps, clock=clock)

    assert sender.calls == [HOME, REMOTE, REMOTE]
    assert sleeps == [pytest.approx(1.0)]
    assert outcome.succeeded


def test_capacity_aware_sends_to_remote_first_when_home_estimated_full():
    tracker, _ = make()
    tracker.reserve(HOME, 5950)
    sender = ScriptedSender({REMOTE: [ok()]})

    outcome = run(sender, tracker, capacity(), estimator=FixedEstimator(1000))

    assert sender.calls == [REMOTE]
    assert outcome.attempts == 1


def test_connection_error_retries_the_same_region_without_switching():
    # 리전 전환은 429 일 때만 한다. 그 외 실패는 같은 리전에 다시 보낸다.
    tracker, _ = make()
    sender = ScriptedSender({HOME: [CallResult(None, {}, None, error="APIConnectionError"), ok()]})

    outcome = run(sender, tracker, spillover())

    assert sender.calls == [HOME, HOME]
    assert (outcome.succeeded, outcome.region, outcome.attempts) == (True, HOME, 2)
    assert tracker.blocked_for(HOME) == 0
    assert tracker.utilization(HOME) == pytest.approx(100 / CAPACITY)


@pytest.mark.parametrize("status", [404, 500, 503])
def test_server_side_failures_retry_the_same_region(status):
    tracker, _ = make()
    sender = ScriptedSender({HOME: [CallResult(status, {}, None), ok()]})

    outcome = run(sender, tracker, spillover())

    assert sender.calls == [HOME, HOME]
    assert (outcome.succeeded, outcome.region) == (True, HOME)
    assert tracker.blocked_for(HOME) == 0


@pytest.mark.parametrize("status", [400, 401, 403])
def test_request_side_failures_stop_immediately(status):
    tracker, _ = make()
    sender = ScriptedSender({HOME: [CallResult(status, {}, None)]})

    outcome = run(sender, tracker, spillover())

    assert (outcome.succeeded, outcome.status, outcome.attempts) == (False, status, 1)
    assert sender.calls == [HOME]


def test_failed_attempt_releases_its_reservation():
    tracker, _ = make()
    sender = ScriptedSender({HOME: [CallResult(400, {}, None, error="BadRequestError")]})

    run(sender, tracker, spillover())

    assert tracker.utilization(HOME) == 0


def test_gives_up_after_max_attempts():
    tracker, _ = make()
    sender = ScriptedSender({HOME: [throttled()] * 2, REMOTE: [throttled()] * 2})

    outcome = run(sender, tracker, spillover(), max_attempts=3)

    assert (outcome.succeeded, outcome.status, outcome.attempts) == (False, 429, 3)


def test_success_without_usage_keeps_estimate():
    tracker, _ = make()
    estimator = FixedEstimator(600)
    sender = ScriptedSender({HOME: [CallResult(200, {}, None)]})

    run(sender, tracker, capacity(), estimator=estimator)

    assert tracker.utilization(HOME) == pytest.approx(600 / CAPACITY)
    assert estimator.observed == []


def test_wait_is_capped_then_sends_anyway():
    tracker, clock = make()
    tracker.mark_throttled(HOME, 3600)
    tracker.mark_throttled(REMOTE, 3600)
    sender = ScriptedSender({HOME: [ok()]})
    sleeps = []

    outcome = run(sender, tracker, spillover(), sleeps=sleeps, clock=clock)

    assert sum(sleeps) == pytest.approx(MAX_WAIT_SECONDS)
    assert outcome.region == HOME


def test_capacity_aware_rechecks_after_waiting():
    # 기다리는 동안 원격 리전이 먼저 비면 그쪽으로 보낸다.
    tracker, clock = make()
    tracker.reserve(HOME, 6000)
    tracker.reserve(REMOTE, 6000)
    tracker.mark_unavailable(HOME, 30)
    sender = ScriptedSender({REMOTE: [ok()]})
    sleeps = []

    outcome = run(sender, tracker, capacity(), estimator=FixedEstimator(1000),
                  sleeps=sleeps, clock=clock)

    assert outcome.region == REMOTE
    assert sum(sleeps) == pytest.approx(10)


def test_concurrent_requests_do_not_overfill_a_region():
    # Arrange: 두 리전 모두 요청 2개 분량만 들어간다. 시간은 흐르지 않는다.
    tracker, _ = make()
    estimator = FixedEstimator(3000)
    barrier = threading.Barrier(4)
    sent = []
    lock = threading.Lock()

    def send(region):
        with lock:
            sent.append(region)
        return CallResult(503, {}, None)  # 예약이 풀리지 않도록 결과는 버린다

    def slow_pick(tracker, order, estimate):
        choice = capacity()(tracker, order, estimate)
        time.sleep(0.01)  # 고르기와 예약 사이에 다른 스레드가 끼어들 틈을 준다
        return choice

    def worker():
        barrier.wait()
        route_request(send, ORDER, tracker, estimator, slow_pick,
                      max_attempts=1, sleep=lambda s: None)

    tracker.settle = lambda *a, **k: None
    tracker.release = lambda *a, **k: None

    # Act
    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    # Assert
    assert sorted(sent) == sorted([HOME, HOME, REMOTE, REMOTE])




def test_waiting_request_uses_estimate_updated_by_other_requests():
    # Arrange: 첫 추정치로는 두 리전 모두 넘친다. 기다리는 동안 다른 요청의 응답으로
    # 추정치가 작아지면, 옛 추정치로 더 기다리지 않고 바로 보낸다.
    tracker, clock = make()
    tracker.reserve(HOME, 5500)
    tracker.reserve(REMOTE, 5500)
    estimator = FixedEstimator(3000)
    sender = ScriptedSender({HOME: [ok()]})
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        estimator.value = 100  # 대기 중에 다른 요청이 실제 비용을 알려줬다
        clock.advance(0.1)

    # Act
    outcome = route_request(sender, ORDER, tracker, estimator, capacity(),
                            max_attempts=1, sleep=sleep)

    # Assert
    assert outcome.region == HOME
    assert len(sleeps) == 1


def test_long_wait_is_split_into_short_rechecks():
    # 한 번에 오래 자면 그 사이 생긴 여유를 놓친다. 짧게 끊어 다시 확인한다.
    tracker, clock = make()
    tracker.mark_throttled(HOME, 5)
    tracker.mark_throttled(REMOTE, 5)
    sender = ScriptedSender({HOME: [ok()]})
    sleeps = []

    run(sender, tracker, spillover(), sleeps=sleeps, clock=clock)

    assert max(sleeps) <= RECHECK_SECONDS
    assert sum(sleeps) == pytest.approx(5)
