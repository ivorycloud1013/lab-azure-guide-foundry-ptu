import threading

import pytest

from conftest import FakeClock
from load_pattern import Batch, paced_batches, requests_per_minute, run_paced


def test_requests_per_minute_divides_ptu_capacity_by_request_cost():
    # 200 PTU x 1,200 = 분당 240,000 정규화 토큰, 이미지 한 장 1,187
    assert requests_per_minute(200, 1200, 1187) == pytest.approx(240000 / 1187)


@pytest.mark.parametrize("ptu, cost", [(0, 1187), (200, 0), (-1, 1187)])
def test_requests_per_minute_rejects_non_positive_inputs(ptu, cost):
    with pytest.raises(ValueError):
        requests_per_minute(ptu, 1200, cost)


def test_paced_batches_split_a_minute_into_even_intervals():
    batches = paced_batches(requests_per_minute=120, interval_seconds=5, duration_seconds=60)

    assert len(batches) == 12
    assert batches[0] == Batch(offset=0.0, size=10)
    assert batches[-1].offset == 55.0
    assert {batch.size for batch in batches} == {10}


def test_paced_batches_carry_fractions_so_the_total_matches_the_rate():
    # 분당 202.2 건 -> 5초당 16.85 건. 반올림 오차가 쌓이지 않아야 한다.
    batches = paced_batches(requests_per_minute=202.2, interval_seconds=5, duration_seconds=180)

    assert sum(batch.size for batch in batches) == 607
    assert {batch.size for batch in batches} <= {16, 17}


def test_paced_batches_rounds_half_up():
    batches = paced_batches(requests_per_minute=30, interval_seconds=5, duration_seconds=10)

    assert [batch.size for batch in batches] == [3, 2]


@pytest.mark.parametrize("rate, interval, duration", [
    (0, 5, 60), (10, 0, 60), (10, 5, 0), (10, 10, 5),
])
def test_paced_batches_reject_invalid_arguments(rate, interval, duration):
    with pytest.raises(ValueError):
        paced_batches(rate, interval, duration)


def test_run_paced_sends_each_batch_at_its_offset_and_keeps_order():
    # Arrange
    clock = FakeClock(start=0.0)
    sleeps = []
    started = []
    changed = threading.Condition()
    submitted_before_sleep = iter([2, 3])

    def sleep(seconds):
        # 앞 묶음의 요청이 모두 시작된 뒤에 시계를 넘겨야 기록 시각이 흔들리지 않는다.
        expected = next(submitted_before_sleep)
        with changed:
            changed.wait_for(lambda: len(started) >= expected, timeout=5)
        sleeps.append(seconds)
        clock.advance(seconds)

    def run_one(index):
        with changed:
            started.append((index, clock()))
            changed.notify_all()
        return index * 10

    batches = (Batch(0.0, 2), Batch(5.0, 1), Batch(10.0, 2))

    # Act
    results = run_paced(batches, run_one, max_workers=4, sleep=sleep, clock=clock)

    # Assert
    assert results == [10, 20, 30, 40, 50]
    assert sleeps == [5.0, 5.0]
    assert sorted(started) == [(1, 0.0), (2, 0.0), (3, 5.0), (4, 10.0), (5, 10.0)]


def test_run_paced_does_not_sleep_when_behind_schedule():
    clock = FakeClock(start=0.0)
    sleeps = []

    def slow_submit_clock():
        clock.advance(3.0)  # 시계를 읽을 때마다 3초씩 늦어진다
        return clock.now

    run_paced((Batch(0.0, 1), Batch(1.0, 1)), lambda i: i, max_workers=1,
              sleep=sleeps.append, clock=slow_submit_clock)

    assert sleeps == []
