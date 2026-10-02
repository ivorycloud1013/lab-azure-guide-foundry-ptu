"""Azure Monitor 값이 어떤 창을 재는지 되찾을 수 있는지 본다.

1차 실험은 표본이 3~8개라 창 길이도 위상도 못 정했다. 60분 실행이면 표본이 60개고
전환 램프가 여러 번 들어오므로, 합성 데이터에서 (창 길이, 위상) 을 되찾을 수 있어야
실제 데이터의 적합 결과를 믿을 수 있다.
"""

import pytest

import metric_fit


CAPACITY = 120_000.0     # 100 PTU x 1200 TPM


def constant_events(start, end, step, cost):
    """step 초마다 cost 만큼 끝나는 작업."""
    at = start
    out = []
    while at < end:
        out.append((at, cost))
        at += step
    return tuple(out)


def test_window_load_is_half_open_on_the_left():
    events = ((100.0, 10.0), (160.0, 10.0))
    # (100, 160] -> 160 만 든다
    assert metric_fit.window_load(events, end=160.0, width=60.0, capacity_tpm=CAPACITY) == \
        pytest.approx(100.0 * 10.0 / CAPACITY)


def test_window_load_normalizes_by_window_length():
    """분당 용량을 꽉 채우는 속도면 창 길이와 무관하게 100% 여야 한다."""
    events = constant_events(0.0, 600.0, step=1.0, cost=CAPACITY / 60.0)
    for width in (10.0, 30.0, 60.0):
        assert metric_fit.window_load(events, end=300.0, width=width,
                                      capacity_tpm=CAPACITY) == pytest.approx(100.0, abs=0.5)


def test_window_load_is_zero_without_events():
    assert metric_fit.window_load((), end=10.0, width=60.0, capacity_tpm=CAPACITY) == 0.0


def test_fit_recovers_the_width_and_phase_used_to_make_the_points():
    # 300초 동안 보내다 멈춘다. 멈춘 뒤 램프가 창 길이를 드러낸다.
    events = constant_events(0.0, 300.0, step=1.0, cost=CAPACITY / 60.0)
    truth_width, truth_phase = 30.0, 20.0
    points = tuple(
        ("aue", float(minute * 60),
         metric_fit.window_load(events, minute * 60 + truth_phase, truth_width, CAPACITY))
        for minute in range(8)
    )
    fit = metric_fit.fit(points, {"aue": events}, {"aue": CAPACITY},
                         widths=(10.0, 30.0, 60.0, 120.0), phase_step=5.0)
    assert fit.width == truth_width
    assert fit.phase == truth_phase
    assert fit.rmse == pytest.approx(0.0, abs=1e-6)


def test_fit_uses_every_region():
    events_a = constant_events(0.0, 300.0, step=1.0, cost=CAPACITY / 60.0)
    events_b = constant_events(0.0, 300.0, step=2.0, cost=CAPACITY / 60.0)
    series = {"aue": events_a, "eus": events_b}
    capacity = {"aue": CAPACITY, "eus": CAPACITY}
    points = tuple(
        (name, float(minute * 60),
         metric_fit.window_load(series[name], minute * 60 + 45.0, 60.0, CAPACITY))
        for name in series for minute in range(6)
    )
    fit = metric_fit.fit(points, series, capacity, widths=(30.0, 60.0), phase_step=15.0)
    assert (fit.width, fit.phase) == (60.0, 45.0)
    assert fit.n == 12


def test_fit_without_points_returns_none():
    assert metric_fit.fit((), {"aue": ()}, {"aue": CAPACITY}) is None


def test_profile_reports_rmse_for_each_width():
    events = constant_events(0.0, 300.0, step=1.0, cost=CAPACITY / 60.0)
    points = tuple(
        ("aue", float(minute * 60),
         metric_fit.window_load(events, minute * 60 + 30.0, 60.0, CAPACITY))
        for minute in range(8)
    )
    profile = metric_fit.width_profile(points, {"aue": events}, {"aue": CAPACITY},
                                       widths=(15.0, 60.0, 240.0), phase_step=10.0)
    best = min(profile, key=lambda row: row.rmse)
    assert best.width == 60.0
    assert [row.width for row in profile] == [15.0, 60.0, 240.0]


def test_split_points_alternates_so_both_halves_span_the_whole_run():
    points = tuple(("aue", float(i * 60), 0.0) for i in range(6))
    train, holdout = metric_fit.split_points(points)
    assert [p[1] for p in train] == [0.0, 120.0, 240.0]
    assert [p[1] for p in holdout] == [60.0, 180.0, 300.0]


def test_rmse_at_measures_a_given_width_and_phase_on_points_it_did_not_choose():
    events = constant_events(0.0, 300.0, step=1.0, cost=CAPACITY / 60.0)
    points = tuple(("aue", float(m * 60),
                    metric_fit.window_load(events, m * 60 + 20.0, 30.0, CAPACITY))
                   for m in range(8))
    assert metric_fit.rmse_at(points, {"aue": events}, {"aue": CAPACITY},
                              width=30.0, phase=20.0) == pytest.approx(0.0, abs=1e-9)
    assert metric_fit.rmse_at(points, {"aue": events}, {"aue": CAPACITY},
                              width=30.0, phase=0.0) > 0.0


def test_rmse_at_without_points_is_none():
    assert metric_fit.rmse_at((), {"aue": ()}, {"aue": CAPACITY}, 60.0, 0.0) is None
