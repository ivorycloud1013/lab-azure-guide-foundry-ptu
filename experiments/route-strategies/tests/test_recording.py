"""실행 기록·사용률 표본·메트릭 조회 테스트.

3분 실험이 결론을 못 낸 원인이 '절대 시각 미기록'이었으므로, 시각이 실제로
남는지를 중심으로 본다.
"""

import json
import subprocess

import metrics
import pytest
import run_record
from capacity_tracker import CapacityTracker
from routing import CallResult, Primary, pickers_for, route_request
from sampler import UtilizationSampler

ORDER = ("aue", "eus")
CAPACITIES = {"aue": 120000.0, "eus": 120000.0}


class FixedEstimator:
    def __init__(self, value=1187.0):
        self.value = value

    def estimate(self):
        return self.value

    def actual_cost(self, usage):
        return self.value

    def observe(self, cost):
        pass


def make_tracker():
    return CapacityTracker(CAPACITIES)


# --- routing: 시도별 절대 시각 ---

def test_attempt_log_records_one_entry_per_attempt():
    # Arrange
    ticks = iter([100.0, 114.0, 120.0, 134.0])
    tracker = make_tracker()
    pick = pickers_for(Primary(ORDER))["429-spillover"]
    calls = []

    def send(region):
        calls.append(region)
        if len(calls) == 1:
            return CallResult(429, {"retry-after-ms": "500"})
        return CallResult(200, {}, {"input_tokens": 11, "output_tokens": 196})

    # Act
    outcome = route_request(send, ORDER, tracker, FixedEstimator(), pick,
                            max_attempts=3, sleep=lambda s: None,
                            wall_clock=lambda: next(ticks))

    # Assert
    assert outcome.succeeded
    assert len(outcome.attempt_log) == 2
    assert [a.region for a in outcome.attempt_log] == ["aue", "eus"]
    assert [a.status for a in outcome.attempt_log] == [429, 200]
    assert outcome.attempt_log[0].sent_at == 100.0
    assert outcome.attempt_log[0].done_at == 114.0


def test_attempt_log_kept_when_request_fails():
    tracker = make_tracker()
    pick = pickers_for(Primary(ORDER))["429-spillover"]
    outcome = route_request(lambda r: CallResult(400, {}, None, "BadRequest"),
                            ORDER, tracker, FixedEstimator(), pick,
                            max_attempts=3, sleep=lambda s: None, wall_clock=lambda: 7.0)
    assert not outcome.succeeded
    assert len(outcome.attempt_log) == 1
    assert outcome.attempt_log[0].sent_at == 7.0


# --- sampler ---

def test_sampler_records_levels_with_absolute_time():
    tracker = make_tracker()
    tracker.reserve("aue", 60000.0)
    sampler = UtilizationSampler(tracker, ORDER, clock=lambda: 1234.5)
    entry = sampler.sample_once()
    assert entry["at"] == 1234.5
    assert entry["levels"]["aue"] == pytest.approx(50.0, abs=0.5)
    assert entry["levels"]["eus"] == 0.0


def test_sampler_stop_appends_final_sample_without_start():
    sampler = UtilizationSampler(make_tracker(), ORDER, clock=lambda: 1.0)
    assert len(sampler.stop()) == 1


def test_sampler_rejects_non_positive_interval():
    with pytest.raises(ValueError):
        UtilizationSampler(make_tracker(), ORDER, interval=0)


# --- metrics ---

def test_window_pads_and_snaps_to_minute_boundaries():
    # 2026-09-17T21:52:30Z = 1789681950
    start, end = metrics.window(1789681950.0, 1789681950.0 + 180, pad=300)
    assert start == "2026-09-17T21:47:00Z"
    assert end == "2026-09-17T22:01:00Z"


def test_window_rejects_reversed_range():
    with pytest.raises(ValueError):
        metrics.window(100.0, 50.0)


def test_build_command_uses_pt1m_and_deployment_split():
    command = metrics.build_command("/subscriptions/x", "A", "B")
    assert "--interval" in command and command[command.index("--interval") + 1] == "PT1M"
    assert metrics.METRIC_NAME in command
    assert "ModelDeploymentName eq '*'" in command


def test_build_command_rejects_empty_resource():
    with pytest.raises(ValueError):
        metrics.build_command("", "A", "B")


def test_parse_metrics_flattens_and_drops_empty_points():
    payload = {"value": [{"timeseries": [{
        "metadatavalues": [{"name": {"value": "ModelDeploymentName"}, "value": "gpt-image-2"}],
        "data": [{"timeStamp": "t0", "average": 79.1, "maximum": 79.1, "minimum": 79.1},
                 {"timeStamp": "t1"}]}]}]}
    rows = metrics.parse_metrics(payload)
    assert len(rows) == 1
    assert rows[0] == {"deployment": "gpt-image-2", "timestamp": "t0",
                       "average": 79.1, "maximum": 79.1, "minimum": 79.1}


def test_parse_metrics_handles_empty_payload():
    assert metrics.parse_metrics({}) == []


def test_fetch_returns_none_when_az_fails():
    def runner(command, **kwargs):
        return subprocess.CompletedProcess(command, 1, "", "not logged in")
    assert metrics.fetch("/subscriptions/x", "A", "B", runner=runner) is None


def test_fetch_returns_none_when_az_missing():
    def runner(command, **kwargs):
        raise FileNotFoundError
    assert metrics.fetch("/subscriptions/x", "A", "B", runner=runner) is None


def test_fetch_returns_none_on_bad_json():
    def runner(command, **kwargs):
        return subprocess.CompletedProcess(command, 0, "not json", "")
    assert metrics.fetch("/subscriptions/x", "A", "B", runner=runner) is None


def test_fetch_all_skips_regions_without_resource_id():
    seen = []

    def runner(command, **kwargs):
        seen.append(command[command.index("--resource") + 1])
        return subprocess.CompletedProcess(command, 0, json.dumps({"value": []}), "")

    result = metrics.fetch_all({"aue": "/subscriptions/a", "eus": None},
                               1789681950.0, 1789682130.0, runner=runner)
    assert seen == ["/subscriptions/a"]
    assert set(result) == {"aue"}


def test_fetch_all_returns_empty_without_any_resource_id():
    result = metrics.fetch_all({"aue": None}, 1.0, 2.0, runner=lambda *a, **k: None)
    assert result == {}


# --- run_record ---

class FakeOutcome:
    def __init__(self):
        self.succeeded = True
        self.region = "eus"
        self.attempts = 1
        self.status = 200
        self.attempt_log = ()


def test_build_adds_iso_timestamps_and_requests():
    record = run_record.build(
        run={"strategy": "capacity-aware", "started_at": 1789681950.0,
             "finished_at": 1789682130.0},
        regions={"eus": {"ptu": 100}},
        outcomes=[FakeOutcome()],
        metrics={"eus": []})
    assert record["schema"] == run_record.SCHEMA_VERSION
    assert record["run"]["started_at_iso"].startswith("2026-09-17T21:52:30")
    assert record["requests"][0]["index"] == 1
    assert record["requests"][0]["status"] == 200


def test_write_round_trips(tmp_path):
    target = tmp_path / "run.json"
    record = run_record.build(run={"started_at": 1.0, "finished_at": 2.0},
                              regions={}, outcomes=[])
    run_record.write(target, record)
    assert json.loads(target.read_text(encoding="utf-8"))["schema"] == 1
