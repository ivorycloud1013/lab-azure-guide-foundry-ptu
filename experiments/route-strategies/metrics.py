"""실행 구간의 Azure Monitor PTU 사용률을 받아 온다.

메트릭은 배포가 아니라 Foundry 리소스에 쌓이므로 배포를 지운 뒤에도 조회된다.
그래도 실행 직후에 받아 두면 실행 기록과 같은 파일에 들어가 분석이 단순해진다.

PT1M 이 가장 짧은 집계 간격이다(PT1M·PT5M·PT15M·PT30M·PT1H·PT6H·PT12H·P1D).
지원 집계는 Minimum·Maximum·Average 뿐이고 Total 이 없다. 누적량이 아니라 순간값을
읽는 지표라는 뜻이므로, 클라이언트의 버킷 수위와 견주는 것이 맞다.
"""

import json
import logging
import subprocess
from datetime import datetime, timedelta, timezone

log = logging.getLogger("multi-region")

METRIC_NAME = "AzureOpenAIProvisionedManagedUtilizationV2"
INTERVAL = "PT1M"
AGGREGATIONS = ("Average", "Maximum", "Minimum")
# 마지막 요청의 응답 시간과 메트릭 집계 지연을 감안해 창을 앞뒤로 넓힌다.
WINDOW_PAD_SECONDS = 300
AZ_TIMEOUT_SECONDS = 180


def _minute(epoch_seconds, pad, rounding):
    moment = datetime.fromtimestamp(epoch_seconds + pad, tz=timezone.utc)
    moment = moment.replace(second=0, microsecond=0)
    if rounding > 0:
        moment += timedelta(minutes=1)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def window(started_at, finished_at, pad=WINDOW_PAD_SECONDS):
    """실행 구간을 분 경계까지 넓힌 (시작, 끝) ISO8601 문자열."""
    if finished_at < started_at:
        raise ValueError("끝 시각이 시작 시각보다 이르다")
    return _minute(started_at, -pad, -1), _minute(finished_at, pad, +1)


def build_command(resource_id, start, end, metric=METRIC_NAME, interval=INTERVAL):
    if not resource_id:
        raise ValueError("resource_id 가 비어 있다")
    return [
        "az", "monitor", "metrics", "list",
        "--resource", resource_id,
        "--metric", metric,
        "--start-time", start,
        "--end-time", end,
        "--interval", interval,
        "--aggregation", *AGGREGATIONS,
        "--filter", "ModelDeploymentName eq '*'",
        "--output", "json",
    ]


def parse_metrics(payload):
    """az 응답을 [{deployment, timestamp, average, maximum, minimum}] 로 편다.

    값이 하나도 없는 점은 버린다. average 와 maximum 이 같으면 그 분의 원시 샘플이
    하나라는 뜻이라, 분석에서 게이지인지 판단하는 근거가 된다.
    """
    rows = []
    for metric in payload.get("value") or []:
        for series in metric.get("timeseries") or []:
            meta = {item["name"]["value"].lower(): item["value"]
                    for item in series.get("metadatavalues") or []}
            for point in series.get("data") or []:
                values = {key: point.get(key) for key in ("average", "maximum", "minimum")}
                if all(value is None for value in values.values()):
                    continue
                rows.append({"deployment": meta.get("modeldeploymentname"),
                             "timestamp": point.get("timeStamp"), **values})
    return rows


def fetch(resource_id, start, end, runner=subprocess.run):
    """az CLI 로 한 리소스의 메트릭을 받아 온다. 실패해도 예외를 올리지 않는다."""
    command = build_command(resource_id, start, end)
    try:
        done = runner(command, capture_output=True, text=True, timeout=AZ_TIMEOUT_SECONDS)
    except FileNotFoundError:
        log.warning("az CLI 를 찾지 못해 메트릭을 건너뛴다")
        return None
    except subprocess.TimeoutExpired:
        log.warning("메트릭 조회가 %s초를 넘겼다: %s", AZ_TIMEOUT_SECONDS, resource_id)
        return None
    if done.returncode != 0:
        log.warning("메트릭 조회 실패 (%s): %s", done.returncode, (done.stderr or "").strip()[:300])
        return None
    try:
        return parse_metrics(json.loads(done.stdout))
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        log.warning("메트릭 응답을 읽지 못했다: %s", exc)
        return None


def fetch_all(resource_ids, started_at, finished_at, runner=subprocess.run):
    """리전 이름 -> 메트릭 행 목록. resource_id 가 없는 리전은 건너뛴다."""
    targets = {name: rid for name, rid in resource_ids.items() if rid}
    if not targets:
        log.info("regions.json 에 resourceId 가 없어 메트릭을 받지 않는다")
        return {}
    start, end = window(started_at, finished_at)
    log.info("메트릭 조회 %s ~ %s | 대상 %s", start, end, ", ".join(sorted(targets)))
    results = {}
    for name, resource_id in targets.items():
        rows = fetch(resource_id, start, end, runner=runner)
        if rows is not None:
            results[name] = rows
            log.info("  %s: %s개 점", name, len(rows))
    return results
