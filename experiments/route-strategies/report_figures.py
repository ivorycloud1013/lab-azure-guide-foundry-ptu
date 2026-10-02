"""실행 기록을 차트가 그대로 읽는 DATA·METRIC 모양으로 묶는다.

report_data 가 "언제 무엇이 일어났나" 를 복원한다면, 여기는 그것을 한 그림 단위로
모으고 실측값과 예측값의 차까지 붙인다.
"""

from bisect import bisect_right

import metric_fit
import report_data

MINUTE = 60.0


def metric_steps(record, width=MINUTE):
    """리전별로 [[시작 초, 끝 초, 값], ...]. 실행 창 밖은 자른다."""
    origin = report_data.started_at(record)
    span = report_data.duration(record)
    index_of = report_data.region_index(record)
    steps = [[] for _ in index_of]
    for name, stamp, value in report_data.metric_points(record):
        if name not in index_of:
            continue
        start = max(0.0, stamp - origin)
        end = min(span, stamp - origin + width)
        if end <= start:
            continue
        steps[index_of[name]].append([round(start, 1), round(end, 1), round(value, 2)])
    return steps


def residuals(record, fit, window=None):
    """리전별로 [[표본을 떴다고 본 시각, 실측 − 예측 %p], ...].

    window 는 적합에 쓴 (가장 이른, 가장 늦은) 분 버킷 시작 epoch 초다. 적합이 쓰지 않은
    점을 잔차로 보여 주면 그림이 근거보다 넓어지므로 같은 창만 그린다.
    """
    if fit is None:
        return [[] for _ in report_data.region_index(record)]
    origin = report_data.started_at(record)
    index_of = report_data.region_index(record)
    capacity = report_data.capacities(record)
    series = {name: metric_fit.Series(rows)
              for name, rows in report_data.completions(record).items()}
    low, high = window if window else (float("-inf"), float("inf"))
    points = [[] for _ in index_of]
    for name, stamp, value in report_data.metric_points(record):
        if name not in index_of or not low <= stamp <= high:
            continue
        moment = stamp + fit.phase
        predicted = series[name].load(moment, fit.width, capacity[name])
        points[index_of[name]].append([round(moment - origin, 1), round(value - predicted, 2)])
    return points


def figure(record, step, level_step, fit, window=None):
    """한 전략의 차트 데이터 한 덩어리."""
    return {
        "buckets": list(report_data.bucketize(record, step)),
        "levels": [list(series) for series in report_data.level_series(record, level_step)],
        "switches": report_data.switch_times(record, step),
        "residuals": residuals(record, fit, window=window),
    }


def _first_attempts(record):
    return tuple(item for _, ordinal, item in report_data.attempts(record) if ordinal == 1)


def _per_minute(tokens, seconds):
    return tokens / (seconds / MINUTE) if seconds > 0 else 0.0


def summary(record):
    """리포트 본문이 쓰는 머릿수. 보낸 양과 실제로 처리된 양을 따로 센다."""
    names = report_data.region_names(record)
    capacity = report_data.capacities(record)
    span = report_data.duration(record)
    all_attempts = tuple(item for _, _, item in report_data.attempts(record))
    firsts = _first_attempts(record)
    done = report_data.completions(record)

    # 보낸 부하는 '보내는 창'(--duration) 으로 나눠야 한다. 마지막 요청이 끝나기를 기다리는
    # 꼬리까지 나누면, 다 보내고 처리가 밀린 실행이 '덜 보냈다' 로 보인다.
    send_seconds = float(record["run"].get("duration") or span)
    offered = _per_minute(sum(float(a["estimate"]) for a in firsts), send_seconds)
    region_capacity = capacity[names[0]]
    total_capacity = sum(capacity.values())
    served = {name: _per_minute(sum(cost for _, cost in rows), span)
              for name, rows in done.items()}

    return {
        "strategy": record["run"]["strategy"],
        "seconds": span,
        "send_seconds": send_seconds,
        "wall_seconds": span,
        "overrun_seconds": max(0.0, span - send_seconds),
        "requests": len(record["requests"]),
        "attempts": len(all_attempts),
        "succeeded": sum(1 for request in record["requests"] if request["succeeded"]),
        "n429": sum(1 for a in all_attempts if a["status"] == report_data.TOO_MANY_REQUESTS),
        "other_failures": sum(1 for a in all_attempts
                              if a["status"] not in (report_data.OK,
                                                     report_data.TOO_MANY_REQUESTS)),
        "offered_tpm": offered,
        "offered_of_region": 100.0 * offered / region_capacity,
        "offered_of_total": 100.0 * offered / total_capacity,
        "served_tpm": served,
        "served_of_region": {name: 100.0 * value / capacity[name]
                             for name, value in served.items()},
        "served_of_total": 100.0 * sum(served.values()) / total_capacity,
        "latency": _latency(record),
    }


def _latency(record):
    spans = [request["attempt_log"][-1]["done_at"] - request["attempt_log"][0]["sent_at"]
             for request in record["requests"] if request["attempt_log"]]
    if not spans:
        return {}
    return {"n": len(spans),
            "p50": round(report_data.percentile(spans, 0.5), 2),
            "p90": round(report_data.percentile(spans, 0.9), 2),
            "p99": round(report_data.percentile(spans, 0.99), 2),
            "max": round(max(spans), 2)}


# 추정 수위가 이 값을 넘으면 그 리전에는 사실상 자리가 없다고 본다.
FULL_ENOUGH_PERCENT = 95.0


def _level_lookup(record):
    """(시각, 리전) -> 그 시각 직전에 찍힌 추정 사용률."""
    samples = sorted((entry["at"], entry["levels"]) for entry in record.get("levels") or ())
    times = [at for at, _ in samples]

    def at(moment, name):
        if not samples:
            return None
        return samples[max(0, bisect_right(times, moment) - 1)][1].get(name)

    return at


def diagnosis(record):
    """429 가 왜 남았는지 가리키는 수치.

    capacity-aware 는 한도에 닿기 전에 리전을 옮기므로 429 가 0 이어야 한다. 그런데도 429 를
    받았다면 두 가지 중 하나다. 판단이 틀렸거나(추정이 한도를 넘었는데도 보냈다), 장부가
    틀렸다(추정은 한도 아래인데 서비스는 꽉 찼다). 받은 순간의 추정 수위가 그것을 가른다.
    """
    level_at = _level_lookup(record)
    rows = report_data.attempts(record)
    throttled = [a for _, _, a in rows if a["status"] == report_data.TOO_MANY_REQUESTS]
    limit = 100.0 * float(record["run"].get("headroom") or 1.0)
    seen = [level_at(a["sent_at"], a["region"]) for a in throttled]
    with_room = [v for v in seen if v is not None and v < limit]

    missing = [a for _, _, a in rows if a["status"] is None]
    total_capacity = sum(report_data.capacities(record).values())
    span = report_data.duration(record)
    missing_tokens = sum(float(a["estimate"]) for a in missing)

    entries = record.get("levels") or ()
    full = sum(1 for entry in entries
               if entry["levels"] and min(entry["levels"].values()) >= FULL_ENOUGH_PERCENT)

    return {
        "headroom": float(record["run"].get("headroom") or 1.0),
        "n429": len(throttled),
        "n429_with_room": len(with_room),
        "level_at_429": (round(report_data.percentile([v for v in seen if v is not None], 0.5), 1)
                         if any(v is not None for v in seen) else None),
        "no_status": len(missing),
        "no_status_of_total": (100.0 * missing_tokens / (total_capacity * span / MINUTE)
                               if span > 0 else 0.0),
        "saturated_share": 100.0 * full / len(entries) if entries else 0.0,
    }
