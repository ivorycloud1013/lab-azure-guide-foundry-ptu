"""차트가 바로 읽는 모양(DATA·METRIC)으로 묶는 부분."""

import pytest

import metric_fit
import report_figures
from test_report_data import CAPACITY, T0, attempt, record


def metrics(rows):
    return {"aue": [{"timestamp": ts, "average": v, "maximum": v, "minimum": v}
                    for ts, v in rows], "eus": []}


def test_metric_steps_are_placed_relative_to_the_run_start():
    data = record([], metrics=metrics([("2023-11-14T22:13:20+00:00", 40.0)]), duration=180.0)
    steps = report_figures.metric_steps(data)
    assert steps[0] == [[0.0, 60.0, 40.0]]
    assert steps[1] == []


def test_metric_steps_are_clipped_to_the_run_window():
    # 실행 시작 30초 전에 시작하는 분 버킷.
    data = record([], metrics=metrics([("2023-11-14T22:12:50+00:00", 12.0)]), duration=180.0)
    assert report_figures.metric_steps(data)[0] == [[0.0, 30.0, 12.0]]


def test_metric_steps_drop_points_entirely_outside_the_run():
    data = record([], metrics=metrics([("2023-11-14T22:30:00+00:00", 12.0)]), duration=180.0)
    assert report_figures.metric_steps(data)[0] == []


def test_residuals_are_observed_minus_prediction_at_the_inferred_sample_instant():
    # 0~120초 동안 분당 용량의 절반을 처리한다.
    requests = [[attempt("aue", 200, t, t, estimate=CAPACITY / 120.0)] for t in range(120)]
    data = record(requests, metrics=metrics([("2023-11-14T22:14:20+00:00", 60.0)]),
                  duration=180.0)
    fit = metric_fit.Fit(width=60.0, phase=0.0, rmse=0.0, n=1)
    points = report_figures.residuals(data, fit)
    assert len(points[0]) == 1
    moment, delta = points[0][0]
    assert moment == pytest.approx(60.0)          # 분 버킷 시작 + phase
    assert delta == pytest.approx(60.0 - 50.0, abs=1.0)


def test_figure_carries_everything_the_chart_reads():
    data = record([[attempt("aue", 200, 1, 2)], [attempt("eus", 200, 61, 62)]],
                  levels=[{"at": T0 + t, "levels": {"aue": 1.0, "eus": 2.0}} for t in range(121)],
                  duration=120.0)
    fit = metric_fit.Fit(width=60.0, phase=30.0, rmse=1.0, n=1)
    figure = report_figures.figure(data, step=30.0, level_step=10.0, fit=fit)
    assert [b["t"] for b in figure["buckets"]] == [0, 30, 60, 90]
    assert len(figure["levels"]) == 2 and len(figure["levels"][0]) == 13
    assert figure["switches"] == (60.0,)
    assert len(figure["residuals"]) == 2


def test_summary_separates_offered_load_from_what_was_processed():
    """분당 용량의 두 배를 밀어 넣고 절반만 200 을 받는 상황."""
    requests = []
    for t in range(120):
        requests.append([attempt("aue", 200, t, t, estimate=CAPACITY / 60.0)])
        requests.append([attempt("aue", 429, t, t, estimate=CAPACITY / 60.0)])
    data = record(requests, duration=120.0)
    summary = report_figures.summary(data)
    assert summary["requests"] == 240
    assert summary["succeeded"] == 120
    assert summary["n429"] == 120
    # 보낸 것은 한 리전 용량의 200%, 실제로 200 을 받은 것은 100%.
    assert summary["offered_of_region"] == pytest.approx(200.0, abs=2.0)
    assert summary["served_of_region"]["aue"] == pytest.approx(100.0, abs=2.0)
    assert summary["offered_of_total"] == pytest.approx(100.0, abs=1.0)


def test_residuals_only_cover_the_points_the_fit_was_allowed_to_use():
    """다른 실행의 트래픽이 섞인 점은 적합에서 뺐으므로 잔차에도 나오면 안 된다."""
    requests = [[attempt("aue", 200, t, t, estimate=CAPACITY / 120.0)] for t in range(120)]
    rows = [("2023-11-14T22:13:20+00:00", 10.0), ("2023-11-14T22:14:20+00:00", 60.0)]
    data = record(requests, metrics=metrics(rows), duration=180.0)
    fit = metric_fit.Fit(width=60.0, phase=0.0, rmse=0.0, n=1)
    window = (T0 + 60.0, T0 + 120.0)        # 두 번째 점만 남긴다
    points = report_figures.residuals(data, fit, window=window)
    assert len(points[0]) == 1
    assert points[0][0][0] == pytest.approx(60.0)


def test_offered_load_is_measured_over_the_send_window_not_the_drain_tail():
    """보내는 창은 --duration 이고, 마지막 요청이 끝나기를 기다리는 꼬리는 그보다 길다.

    꼬리까지 나누면 '덜 보냈다'로 보이지만, 실제로는 다 보내고 처리가 밀린 것이다.
    """
    # 60초 동안 분당 용량만큼 보내고, 마지막 요청이 60초 더 걸려 끝난다.
    requests = [[attempt("aue", 200, t, t + 60, estimate=CAPACITY / 60.0)] for t in range(60)]
    data = record(requests, duration=120.0)
    data["run"]["duration"] = 60.0               # 설정상 보내는 창
    summary = report_figures.summary(data)
    assert summary["send_seconds"] == 60.0
    assert summary["wall_seconds"] == 120.0
    assert summary["offered_of_region"] == pytest.approx(100.0, abs=1.0)
    assert summary["overrun_seconds"] == pytest.approx(60.0)


def test_diagnosis_separates_throttling_the_client_saw_coming_from_throttling_it_did_not():
    """추정이 한도 아래인데 429 가 왔다면, 틀린 것은 판단이 아니라 장부다."""
    requests = [
        [attempt("aue", 429, 10, 11), attempt("eus", 200, 11, 12)],   # 수위 20%
        [attempt("aue", 429, 30, 31), attempt("eus", 200, 31, 32)],   # 수위 100%
        [attempt("aue", 200, 40, 41)],
    ]
    levels = [{"at": T0 + t, "levels": {"aue": 20.0 if t < 20 else 100.0, "eus": 0.0}}
              for t in range(0, 61)]
    data = record(requests, levels=levels, duration=60.0)
    found = report_figures.diagnosis(data)
    assert found["n429"] == 2
    assert found["n429_with_room"] == 1          # 수위 20% 였던 한 건
    assert found["headroom"] == 1.0


def test_diagnosis_reports_unaccounted_work_from_attempts_without_a_status():
    # 한 리전의 분당 용량만큼을 1분 동안 날렸으면, 두 리전 합계의 50% 가 장부에서 빠진 것이다.
    requests = [[attempt("aue", None, 1, 2, estimate=CAPACITY)]]
    data = record(requests, duration=60.0)
    found = report_figures.diagnosis(data)
    assert found["no_status"] == 1
    assert found["no_status_of_total"] == pytest.approx(50.0, abs=0.1)


def test_diagnosis_measures_how_long_every_region_was_estimated_full():
    levels = [{"at": T0 + t, "levels": {"aue": 99.0, "eus": 99.0 if t >= 30 else 10.0}}
              for t in range(0, 61)]
    data = record([], levels=levels, duration=60.0)
    assert report_figures.diagnosis(data)["saturated_share"] == pytest.approx(50.8, abs=1.0)
