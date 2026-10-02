"""실행 기록을 JSON 한 파일로 남긴다.

3분 실험은 시작 시각을 분 단위로만 남겨, 나중에 Azure Monitor 의 분 단위 값과
맞추려 할 때 정렬을 사후에 추정해야 했다. 그래서 이 기록은 시도마다 절대 UTC
시각을 담는다. 분석은 이 파일만 보고 묶음을 다시 만들 수 있어야 한다.
"""

import json
from datetime import datetime, timezone

SCHEMA_VERSION = 1


def iso(epoch_seconds):
    return datetime.fromtimestamp(epoch_seconds, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _attempt(attempt):
    return {"region": attempt.region, "status": attempt.status,
            "sent_at": round(attempt.sent_at, 3), "done_at": round(attempt.done_at, 3),
            "estimate": round(attempt.estimate, 1)}


def _request(index, outcome):
    return {"index": index, "succeeded": outcome.succeeded, "region": outcome.region,
            "attempts": outcome.attempts, "status": outcome.status,
            "attempt_log": [_attempt(a) for a in outcome.attempt_log]}


def build(run, regions, outcomes, metrics=None):
    """run: 실행 설정 dict. regions: 리전 이름 -> 설정 dict. outcomes: RouteOutcome 목록."""
    started_at = run.get("started_at")
    finished_at = run.get("finished_at")
    return {
        "schema": SCHEMA_VERSION,
        "run": {**run,
                "started_at_iso": iso(started_at) if started_at else None,
                "finished_at_iso": iso(finished_at) if finished_at else None},
        "regions": regions,
        "requests": [_request(i, o) for i, o in enumerate(outcomes, start=1)],
        "metrics": metrics or {},
    }


def write(path, record):
    with open(path, "w", encoding="utf-8") as target:
        json.dump(record, target, ensure_ascii=False, indent=1)
    return path
