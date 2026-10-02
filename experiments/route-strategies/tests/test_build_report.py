"""템플릿 채우기. 자리표시자를 하나라도 놓치면 리포트가 그대로 나가 버리므로 막는다."""

import pytest

import build_report


def test_render_replaces_every_placeholder():
    assert build_report.render("a {{X}} b {{Y}}", {"X": "1", "Y": "2"}) == "a 1 b 2"


def test_render_refuses_to_ship_an_unfilled_placeholder():
    with pytest.raises(ValueError, match="MISSING"):
        build_report.render("{{MISSING}}", {"X": "1"})


def test_render_reports_every_missing_name_at_once():
    with pytest.raises(ValueError) as caught:
        build_report.render("{{A}} {{B}}", {})
    assert "A" in str(caught.value) and "B" in str(caught.value)


def test_render_leaves_unused_fields_alone():
    assert build_report.render("{{X}}", {"X": "1", "UNUSED": "2"}) == "1"


def test_seconds_read_as_minutes_and_seconds():
    assert build_report.hhmm(3600) == "60분 0초"
    assert build_report.hhmm(95) == "1분 35초"


def test_json_block_is_embeddable_in_a_script_tag():
    """</script> 가 값 안에 들어가도 스크립트가 끊기지 않아야 한다."""
    assert "</script>" not in build_report.json_block({"k": "</script>"})


def _run(strategy, same, total):
    """원시 표본이 same/total 인 가짜 실행."""
    rows = [{"timestamp": f"2023-11-14T22:{13 + i:02d}:00Z", "average": 5.0,
             "maximum": 5.0 if i < same else 9.0, "minimum": 5.0 if i < same else 1.0}
            for i in range(total)]
    return {"key": strategy,
            "record": {"schema": 1, "run": {"strategy": strategy, "started_at": 0.0,
                                            "finished_at": 60.0},
                       "regions": {"aue": {"capacity_tpm": 1.0}}, "requests": [],
                       "metrics": {"aue": rows}, "levels": []}}


def test_sample_share_adds_up_every_run_not_just_the_first_and_last():
    runs = [_run("a", 1, 2), _run("b", 2, 2), _run("c", 0, 3)]
    assert build_report.sample_share(runs) == (3, 7)


def test_views_do_not_mutate_the_runs_they_summarize():
    runs = [{"key": "a"}, {"key": "b"}]
    figures = {"a": {"switches": (1.0, 2.0)}, "b": {"switches": ()}}
    views = build_report.views(runs, figures)
    assert [v["switches"] for v in views] == [2, 0]
    assert "switches" not in runs[0] and "switches" not in runs[1]


def _record(started, finished):
    return {"run": {"started_at": started, "finished_at": finished}}


def test_clean_window_drops_the_head_so_every_look_back_is_inside_the_run():
    """창이 실행 시작보다 앞을 보면 예측이 0 이 되어 버린다."""
    low, _ = build_report.clean_window(_record(1000.0, 4600.0), None,
                                       max_width=180.0, max_phase=60.0)
    assert low == 1180.0


def test_clean_window_stops_before_the_next_run_starts():
    """실행 뒤 5분까지 조회하므로, 다음 실행의 트래픽이 섞인 점을 버려야 한다."""
    _, high = build_report.clean_window(_record(1000.0, 4600.0), next_started_at=4800.0,
                                        max_width=180.0, max_phase=60.0)
    assert high == 4800.0 - 60.0 - 60.0


def test_clean_window_keeps_the_tail_of_the_last_run():
    """마지막 실행은 뒤에 섞일 트래픽이 없다. 멈춘 뒤의 램프가 창 길이를 드러낸다."""
    _, high = build_report.clean_window(_record(1000.0, 4600.0), None,
                                        max_width=180.0, max_phase=60.0)
    assert high > 4600.0


def test_in_window_keeps_only_points_inside_the_bounds():
    points = (("aue", 100.0, 1.0), ("aue", 200.0, 2.0), ("aue", 300.0, 3.0))
    assert build_report.in_window(points, 150.0, 250.0) == (("aue", 200.0, 2.0),)


def _full_run(strategy, started, metrics_regions):
    """metrics 에만 있는 리전이 섞인 최소 실행 기록."""
    rows = {name: [{"timestamp": "2023-11-14T22:20:00Z", "average": 5.0,
                    "maximum": 5.0, "minimum": 5.0}] for name in metrics_regions}
    return {"key": strategy, "summary": {},
            "record": {"schema": 1,
                       "run": {"strategy": strategy, "started_at": started,
                               "finished_at": started + 3600.0},
                       "regions": {"aue": {"capacity_tpm": 120000.0}},
                       "requests": [], "metrics": rows, "levels": []}}


def test_combined_fit_ignores_metric_rows_for_regions_outside_the_route():
    """조회가 route 에 없는 리전을 돌려줘도 KeyError 로 죽지 않아야 한다."""
    runs = [_full_run("a", 1_699_999_000.0, ["aue", "weu"])]
    _, best, points, _, _ = build_report.combined_fit(runs, widths=(60.0,), phase_step=30.0)
    assert points == 1          # aue 만 센다
    assert best is not None


def test_build_says_so_when_there_is_no_metric_data_to_fit():
    runs = [_full_run("a", 1_699_999_000.0, [])]
    with pytest.raises(ValueError, match="실측"):
        build_report.build(runs, step=30.0, level_step=5.0, phase_step=30.0, widths=(60.0,))


def test_clean_window_refuses_an_inverted_window_instead_of_dropping_everything():
    """실행 간격이 너무 짧으면 남는 점이 하나도 없다. 조용히 비우지 않는다."""
    with pytest.raises(ValueError, match="간격"):
        build_report.clean_window(_record(1000.0, 1100.0), next_started_at=1150.0,
                                  max_width=180.0, max_phase=60.0)


def test_latency_axis_ignores_a_few_huge_buckets_so_the_normal_range_stays_readable():
    """한 전략의 긴 꼬리가 축을 끌어올리면 다른 전략의 선이 바닥에 깔린다."""
    buckets = [{"rt90": 15.0} for _ in range(100)] + [{"rt90": 1200.0}]
    axis = build_report.axis_max({"a": {"buckets": buckets}}, lambda b: b["rt90"],
                                 floor=10, headroom=1.2, share=0.98)
    assert axis < 100
