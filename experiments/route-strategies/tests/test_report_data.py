"""실행 기록 JSON 을 차트가 읽는 모양으로 바꾸는 부분.

기록 파일 하나만 보고 묶음을 다시 만들 수 있어야 한다는 것이 2차 실험 기록의 전제라,
여기서 검증하는 것도 "기록에 있는 절대 시각만으로 복원되는가" 다.
"""

import pytest

import report_data


T0 = 1_700_000_000.0
CAPACITY = 120_000.0


def attempt(region, status, sent, done, estimate=1000.0):
    return {"region": region, "status": status,
            "sent_at": T0 + sent, "done_at": T0 + done, "estimate": estimate}


def record(requests, levels=(), metrics=None, duration=120.0):
    return {
        "schema": 1,
        "run": {"strategy": "429-spillover", "started_at": T0,
                "finished_at": T0 + duration, "duration": duration,
                "interval": 5.0, "load_ptu": 200.0},
        "regions": {"aue": {"ptu": 100, "capacity_tpm": CAPACITY, "deployment": "d"},
                    "eus": {"ptu": 100, "capacity_tpm": CAPACITY, "deployment": "d"}},
        "requests": [{"index": i, "succeeded": r[-1]["status"] == 200,
                      "region": r[-1]["region"], "attempts": len(r),
                      "status": r[-1]["status"], "attempt_log": list(r)}
                     for i, r in enumerate(requests, start=1)],
        "metrics": metrics or {},
        "levels": list(levels),
    }


def test_region_names_keep_the_route_order():
    assert report_data.region_names(record([])) == ("aue", "eus")


def test_capacities_come_from_the_record_not_from_a_constant():
    assert report_data.capacities(record([])) == {"aue": CAPACITY, "eus": CAPACITY}


def test_completions_only_count_attempts_that_returned_200():
    data = record([[attempt("aue", 429, 0, 1), attempt("eus", 200, 1, 3, estimate=900.0)]])
    done = report_data.completions(data)
    assert done["aue"] == ()
    assert done["eus"] == ((T0 + 3, 900.0),)


def test_buckets_group_attempts_by_ordinal_region_and_status():
    data = record([[attempt("aue", 200, 1, 2)],
                   [attempt("aue", 429, 2, 3), attempt("eus", 200, 3, 4)],
                   [attempt("aue", 200, 4, 5)]], duration=10.0)
    buckets = report_data.bucketize(data, step=10.0)
    assert len(buckets) == 1
    assert sorted(buckets[0]["segs"]) == [[1, 0, 200, 2], [1, 0, 429, 1], [2, 1, 200, 1]]


def test_buckets_cover_the_whole_run_even_where_nothing_was_sent():
    data = record([[attempt("aue", 200, 1, 2)]], duration=90.0)
    buckets = report_data.bucketize(data, step=30.0)
    assert [b["t"] for b in buckets] == [0, 30, 60]
    assert buckets[1]["segs"] == [] and buckets[1]["rt"] is None


def test_bucket_primary_is_the_region_most_first_attempts_went_to():
    data = record([[attempt("aue", 200, 1, 2)],
                   [attempt("eus", 200, 2, 3)],
                   [attempt("eus", 200, 3, 4)]], duration=10.0)
    assert report_data.bucketize(data, step=10.0)[0]["primary"] == 1


def test_bucket_latency_is_measured_from_first_send_to_last_done():
    data = record([[attempt("aue", 429, 0, 1), attempt("eus", 200, 1, 5)]], duration=10.0)
    bucket = report_data.bucketize(data, step=10.0)[0]
    assert bucket["rt"] == pytest.approx(5.0)
    assert bucket["n429req"] == 1
    # 마지막 시도만 걸렸다면 4초였을 텐데 5초 걸렸으니 429 가 1초를 더했다.
    assert bucket["extra"] == pytest.approx(1.0)


def test_bucket_rolling_load_is_the_last_60_seconds_at_the_bucket_end():
    # 60초 동안 분당 용량의 절반을 처리한다.
    requests = [[attempt("aue", 200, t, t, estimate=CAPACITY / 120.0)] for t in range(60)]
    data = record(requests, duration=60.0)
    bucket = report_data.bucketize(data, step=60.0)[0]
    assert bucket["roll"][0] == pytest.approx(50.0, abs=1.0)
    assert bucket["roll"][1] == 0.0


def test_level_series_is_sampled_on_a_regular_grid_from_the_run_start():
    levels = [{"at": T0 + t, "levels": {"aue": float(t), "eus": 0.0}} for t in range(0, 31)]
    data = record([], levels=levels, duration=30.0)
    series = report_data.level_series(data, step=10.0)
    assert series[0] == (0.0, 10.0, 20.0, 30.0)
    assert series[1] == (0.0, 0.0, 0.0, 0.0)


def test_level_series_holds_the_last_known_value_between_samples():
    levels = [{"at": T0, "levels": {"aue": 5.0, "eus": 0.0}},
              {"at": T0 + 25, "levels": {"aue": 9.0, "eus": 0.0}}]
    data = record([], levels=levels, duration=30.0)
    assert report_data.level_series(data, step=10.0)[0] == (5.0, 5.0, 5.0, 9.0)


def test_switch_times_mark_where_the_primary_changes():
    data = record([[attempt("aue", 200, 1, 2)],
                   [attempt("eus", 200, 31, 32)],
                   [attempt("aue", 200, 61, 62)]], duration=90.0)
    assert report_data.switch_times(data, step=30.0) == (30.0, 60.0)


def test_metric_points_turn_azure_timestamps_into_epoch_seconds():
    metrics = {"aue": [{"timestamp": "2023-11-14T22:13:00+00:00",
                        "average": 12.5, "maximum": 12.5, "minimum": 12.5}]}
    points = report_data.metric_points(record([], metrics=metrics))
    assert points == (("aue", 1_699_999_980.0, 12.5),)


def test_metric_points_drop_rows_without_a_value():
    metrics = {"aue": [{"timestamp": "2023-11-14T22:13:00Z",
                        "average": None, "maximum": None, "minimum": None}]}
    assert report_data.metric_points(record([], metrics=metrics)) == ()


def test_single_sample_minutes_are_counted_separately():
    """average 와 max·min 이 모두 같으면 그 분의 원시 표본이 하나라는 뜻이다."""
    metrics = {"aue": [{"timestamp": "2023-11-14T22:13:00Z", "average": 5.0,
                        "maximum": 5.0, "minimum": 5.0},
                       {"timestamp": "2023-11-14T22:14:00Z", "average": 5.0,
                        "maximum": 9.0, "minimum": 1.0}]}
    assert report_data.single_sample_share(record([], metrics=metrics)) == (1, 2)


def test_buckets_handle_attempts_that_never_got_a_status():
    """연결 실패·타임아웃은 status 가 None 이다. 정렬에서 터지면 안 된다."""
    data = record([[attempt("aue", None, 1, 2), attempt("aue", 200, 2, 3)],
                   [attempt("aue", 200, 1, 2)]], duration=10.0)
    segs = report_data.bucketize(data, step=10.0)[0]["segs"]
    assert [1, 0, None, 1] in segs
    # None 이 먼저, 그다음 상태 코드 순.
    assert segs[0][2] is None


def test_completions_ignore_attempts_without_a_status():
    data = record([[attempt("aue", None, 1, 2), attempt("aue", 200, 2, 3, estimate=7.0)]])
    assert report_data.completions(data)["aue"] == ((T0 + 3, 7.0),)


def test_metric_points_drop_a_repeated_minute_for_the_same_region():
    """재조회가 겹치면 같은 분이 두 번 올 수 있다. 두 번 세면 적합이 그쪽으로 끌린다."""
    row = {"timestamp": "2023-11-14T22:13:00Z", "average": 5.0, "maximum": 5.0, "minimum": 5.0}
    data = record([], metrics={"aue": [row, dict(row)]})
    assert len(report_data.metric_points(data)) == 1


def test_percentile_refuses_an_empty_list_with_a_clear_error():
    with pytest.raises(ValueError, match="비어"):
        report_data.percentile([], 0.5)
