"""실행 기록 JSON 을 리포트 차트가 읽는 모양으로 바꾼다.

기록에는 시도마다 절대 UTC 시각이 들어 있다. 그래서 여기서 하는 일은 모두 "실행 시작
시각을 0으로 놓고 다시 묶기" 이고, 1차 실험처럼 정렬을 사후에 추정하는 단계가 없다.
"""

import json
from bisect import bisect_right
from collections import Counter
from datetime import datetime

import metric_fit

OK = 200
TOO_MANY_REQUESTS = 429
ROLLING_WINDOW_SECONDS = 60.0
SCHEMA_VERSION = 1


def load(path):
    with open(path, encoding="utf-8") as source:
        record = json.load(source)
    validate(record)
    return record


def validate(record):
    """분석이 기대하는 것만 확인한다. 없는 키를 나중에 KeyError 로 만나지 않게 한다."""
    if not isinstance(record, dict):
        raise ValueError("기록 최상위는 객체여야 한다")
    if record.get("schema") != SCHEMA_VERSION:
        raise ValueError(f"schema {record.get('schema')!r} 는 이 분석기가 모른다")
    run = record.get("run")
    if not isinstance(run, dict):
        raise ValueError("run 이 없다")
    for key in ("started_at", "finished_at", "strategy"):
        if run.get(key) is None:
            raise ValueError(f"run.{key} 가 없다")
    if run["finished_at"] < run["started_at"]:
        raise ValueError("finished_at 이 started_at 보다 이르다")
    if not isinstance(record.get("regions"), dict) or not record["regions"]:
        raise ValueError("regions 가 비어 있다")
    if not isinstance(record.get("requests"), list):
        raise ValueError("requests 가 없다")
    return record


def started_at(record):
    return record["run"]["started_at"]


def duration(record):
    return record["run"]["finished_at"] - record["run"]["started_at"]


def region_names(record):
    return tuple(record["regions"])


def region_index(record):
    return {name: index for index, name in enumerate(region_names(record))}


def capacities(record):
    return {name: float(data["capacity_tpm"]) for name, data in record["regions"].items()}


def attempts(record):
    """(요청, 시도 순번, 시도) 를 보낸 순서대로."""
    rows = []
    for request in record["requests"]:
        for ordinal, item in enumerate(request.get("attempt_log") or (), start=1):
            rows.append((request, ordinal, item))
    rows.sort(key=lambda row: row[2]["sent_at"])
    return tuple(rows)


def completions(record):
    """리전 이름 -> ((끝난 시각, 정규화 토큰), ...). 200 을 받은 시도만 센다."""
    done = {name: [] for name in region_names(record)}
    for _, _, item in attempts(record):
        if item["status"] == OK and item["region"] in done:
            done[item["region"]].append((item["done_at"], float(item["estimate"])))
    return {name: tuple(sorted(rows)) for name, rows in done.items()}


def parse_timestamp(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()


def _metric_value(row):
    for key in ("average", "maximum", "minimum"):
        if row.get(key) is not None:
            return float(row[key])
    return None


def metric_points(record):
    """(리전, 분 버킷 시작 epoch 초, 값 %) 을 시각 순서로."""
    # 조회 창이 겹치면 같은 (리전, 분) 이 두 번 올 수 있다. 두 번 세면 적합이 그 점으로 끌린다.
    seen = {}
    for name, rows in (record.get("metrics") or {}).items():
        for row in rows or ():
            value = _metric_value(row)
            if value is None or not row.get("timestamp"):
                continue
            seen.setdefault((name, parse_timestamp(row["timestamp"])), value)
    return tuple((name, stamp, value)
                 for (name, stamp), value in sorted(seen.items(), key=lambda item:
                                                    (item[0][1], item[0][0])))


def single_sample_share(record):
    """(average 와 max·min 이 모두 같은 분의 수, 값이 있는 분의 수).

    셋이 같다는 것은 그 분의 원시 표본이 하나라는 뜻이고, 이 지표가 분 안의 여러 값을
    모은 것이 아니라 한 번 뜬 점이라는 근거가 된다.
    """
    same = total = 0
    for rows in (record.get("metrics") or {}).values():
        for row in rows or ():
            if _metric_value(row) is None:
                continue
            total += 1
            values = [row.get(key) for key in ("average", "maximum", "minimum")]
            if all(value is not None for value in values) and len(set(values)) == 1:
                same += 1
    return same, total


def percentile(values, share):
    """가장 가까운 순위 방식."""
    if not values:
        raise ValueError("percentile: 값이 비어 있다")
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(share * (len(ordered) - 1))))
    return ordered[index]


def _request_span(request):
    log = request["attempt_log"]
    return log[0]["sent_at"], log[-1]["done_at"]


def _extra_from_429(request):
    """429 때문에 늘어난 시간. 마지막 시도만 있었다면 걸렸을 시간과의 차이."""
    log = request["attempt_log"]
    first_sent, last_done = _request_span(request)
    last = log[-1]
    return (last_done - first_sent) - (last["done_at"] - last["sent_at"])


# 연결 실패·타임아웃은 status 가 None 이라 상태 코드와 직접 견줄 수 없다. 맨 앞에 놓는다.
NO_STATUS_ORDER = -1


def _segment_order(key):
    ordinal, region, status = key
    return ordinal, region, NO_STATUS_ORDER if status is None else status


def _bucket_segments(rows, index_of):
    """(시도 순번, 리전 번호, 상태) 별 건수. 시도 순번 -> 리전 -> 상태 순으로 정렬한다."""
    counts = Counter((ordinal, index_of[item["region"]], item["status"])
                     for _, ordinal, item in rows if item["region"] in index_of)
    return [[ordinal, region, status, count]
            for (ordinal, region, status), count in
            sorted(counts.items(), key=lambda item: _segment_order(item[0]))]


def _bucket_primary(rows, index_of):
    firsts = Counter(index_of[item["region"]]
                     for _, ordinal, item in rows if ordinal == 1 and item["region"] in index_of)
    if not firsts:
        return None
    return firsts.most_common(1)[0][0]


def bucket_count(record, step):
    if step <= 0:
        raise ValueError("step 은 0 보다 커야 한다")
    span = duration(record)
    return max(1, int(-(-span // step)))


def bucketize(record, step):
    """step 초 구간마다 시도·primary·응답 시간·최근 60초 처리량을 모은다."""
    origin = started_at(record)
    index_of = region_index(record)
    names = region_names(record)
    series = {name: metric_fit.Series(rows) for name, rows in completions(record).items()}
    capacity = capacities(record)

    by_bucket = {}
    starts = {}
    for request, ordinal, item in attempts(record):
        slot = int((item["sent_at"] - origin) // step)
        by_bucket.setdefault(slot, []).append((request, ordinal, item))
        if ordinal == 1:
            starts.setdefault(slot, []).append(request)

    buckets = []
    for slot in range(bucket_count(record, step)):
        rows = by_bucket.get(slot, [])
        begun = starts.get(slot, [])
        spans = [_request_span(request) for request in begun]
        latency = [done - sent for sent, done in spans]
        throttled = [request for request in begun
                     if any(a["status"] == TOO_MANY_REQUESTS for a in request["attempt_log"])]
        extras = [_extra_from_429(request) for request in throttled]
        end = origin + (slot + 1) * step
        buckets.append({
            "t": int(slot * step),
            "segs": _bucket_segments(rows, index_of),
            "primary": _bucket_primary(rows, index_of),
            "rt": round(percentile(latency, 0.5), 2) if latency else None,
            "rt90": round(percentile(latency, 0.9), 2) if latency else None,
            "roll": [round(series[name].load(end, ROLLING_WINDOW_SECONDS, capacity[name]), 1)
                     for name in names],
            "extra": round(percentile(extras, 0.5), 2) if extras else None,
            "n429req": len(throttled),
            "n": len(begun),
        })
    return tuple(buckets)


def level_series(record, step):
    """리전별 추정 사용률(%)을 실행 시작부터 step 초 격자 위에 올린다."""
    if step <= 0:
        raise ValueError("step 은 0 보다 커야 한다")
    origin = started_at(record)
    names = region_names(record)
    samples = sorted((entry["at"] - origin, entry["levels"])
                     for entry in record.get("levels") or ())
    times = [at for at, _ in samples]
    points = int(duration(record) // step) + 1

    def value_at(moment, name):
        if not samples:
            return 0.0
        position = bisect_right(times, moment) - 1
        if position < 0:
            position = 0
        return float(samples[position][1].get(name, 0.0))

    return tuple(tuple(value_at(index * step, name) for index in range(points))
                 for name in names)


def switch_times(record, step):
    """primary 가 바뀐 구간의 시작 시각."""
    buckets = bucketize(record, step)
    times, previous = [], None
    for bucket in buckets:
        if bucket["primary"] is None:
            continue
        if previous is not None and bucket["primary"] != previous:
            times.append(float(bucket["t"]))
        previous = bucket["primary"]
    return tuple(times)
