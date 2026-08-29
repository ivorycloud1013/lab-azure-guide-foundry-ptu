#!/usr/bin/env python3
"""PTU 배포의 429 를 클라이언트에서 직접 재시도한다.

PTU 배포는 사용률이 100% 에 닿으면 큐잉하지 않고 즉시 429 를 돌려주며,
``retry-after-ms`` / ``retry-after`` 헤더로 "언제 다시 오면 되는지" 를 알려준다.
이 스크립트는 그 헤더를 그대로 신뢰해 대기 시간을 정하고, 헤더가 없을 때만
지수 백오프 + 지터로 폴백한다.

SDK 자동 재시도(max_retries)는 꺼져 있다. 매 시도의 응답 헤더를 직접 보기 위해서다.

FOUNDRY_BURST 를 2 이상으로 주면 동시 요청을 날려 429 를 실제로 유발할 수 있다.

사용법:
    python foundry-ptu-429-retry.py
    FOUNDRY_BURST=20 FOUNDRY_MAX_ATTEMPTS=6 python foundry-ptu-429-retry.py
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass

from foundry_ptu_common import (
    PRINT_LOCK,
    RETRYABLE_STATUS_CODES,
    CallResult,
    ConfigError,
    Settings,
    build_client,
    call_deployment,
    describe_payload,
    fail,
    load_settings,
    positive_int_env,
    print_response_headers,
    print_spillover_verdict,
    retry_after_seconds,
    run_workers,
)

# ---------------------------------------------------------------------------
# 재시도 정책 상수
# ---------------------------------------------------------------------------

DEFAULT_MAX_ATTEMPTS = 5
DEFAULT_BURST_SIZE = 1

#: retry-after 헤더가 없을 때만 쓰는 지수 백오프 기준값(초).
BASE_BACKOFF_SECONDS = 1.0
BACKOFF_MULTIPLIER = 2.0
MAX_BACKOFF_SECONDS = 30.0

#: 동시 요청이 같은 시각에 몰려 재차 429 를 맞는 것을 막기 위한 지터 비율.
JITTER_RATIO = 0.25

#: 헤더가 비정상적으로 큰 값을 주더라도 이 이상은 기다리지 않는다.
MAX_SLEEP_SECONDS = 60.0


@dataclass(frozen=True)
class RetryOutcome:
    """워커 하나의 최종 결과."""

    worker_id: int
    attempts: int
    total_wait_seconds: float
    result: CallResult


def compute_wait_seconds(result: CallResult, attempt_index: int) -> float:
    """다음 시도까지 기다릴 시간을 계산한다.

    서비스가 알려준 retry-after 를 최우선으로 쓴다. PTU 의 leaky bucket 사용률이
    언제 내려가는지는 서비스만 알고 있으므로, 임의 백오프보다 정확하다.

    Args:
        result: 방금 받은 실패 응답.
        attempt_index: 0부터 시작하는 시도 번호.
    """
    header_wait = retry_after_seconds(result.headers)
    if header_wait is not None:
        base = header_wait
    else:
        base = min(
            BASE_BACKOFF_SECONDS * (BACKOFF_MULTIPLIER ** attempt_index),
            MAX_BACKOFF_SECONDS,
        )

    jitter = base * JITTER_RATIO * random.random()
    return min(base + jitter, MAX_SLEEP_SECONDS)


def run_with_retry(settings: Settings, worker_id: int, max_attempts: int) -> RetryOutcome:
    """한 요청을 최대 max_attempts 번까지 재시도한다."""
    client = build_client(settings, settings.ptu)
    total_wait = 0.0
    result = None

    for attempt_index in range(max_attempts):
        attempt_number = attempt_index + 1
        result = call_deployment(client, settings, settings.ptu)

        with PRINT_LOCK:
            print_response_headers(
                result,
                title=f"worker {worker_id} / 시도 {attempt_number}/{max_attempts}",
            )
            print_spillover_verdict(result)

        if result.is_success:
            return RetryOutcome(worker_id, attempt_number, total_wait, result)

        if result.status_code not in RETRYABLE_STATUS_CODES:
            with PRINT_LOCK:
                print(f"\n[worker {worker_id}] 재시도 대상이 아닌 상태 코드 {result.status_code} → 중단")
            return RetryOutcome(worker_id, attempt_number, total_wait, result)

        if attempt_number == max_attempts:
            break

        wait_seconds = compute_wait_seconds(result, attempt_index)
        source = "retry-after 헤더" if retry_after_seconds(result.headers) is not None else "지수 백오프"
        with PRINT_LOCK:
            print(f"\n[worker {worker_id}] {result.status_code} 수신 → {wait_seconds:.3f}초 대기 ({source})")

        time.sleep(wait_seconds)
        total_wait += wait_seconds

    return RetryOutcome(worker_id, max_attempts, total_wait, result)


def print_summary(outcomes: list[RetryOutcome], settings: Settings) -> None:
    print("\n" + "=" * 78)
    print("요약")
    print("=" * 78)
    for outcome in sorted(outcomes, key=lambda item: item.worker_id):
        verdict = "성공" if outcome.result.is_success else f"실패({outcome.result.status_code})"
        body = describe_payload(outcome.result, settings) if outcome.result.is_success else ""
        print(
            f"  worker {outcome.worker_id:>3} | {verdict:<10} | 시도 {outcome.attempts}회 "
            f"| 총 대기 {outcome.total_wait_seconds:.3f}초 | {body}"
        )

    succeeded = sum(1 for outcome in outcomes if outcome.result.is_success)
    print(f"\n  성공 {succeeded} / 전체 {len(outcomes)}")


def main() -> int:
    try:
        settings = load_settings()
        max_attempts = positive_int_env("FOUNDRY_MAX_ATTEMPTS", DEFAULT_MAX_ATTEMPTS)
        burst_size = positive_int_env("FOUNDRY_BURST", DEFAULT_BURST_SIZE)
    except ConfigError as exc:
        fail(str(exc))
        return 1

    print(f"mode        : {settings.mode}")
    print(f"ptu 배포    : {settings.ptu.deployment}")
    print(f"동시 요청   : {burst_size}")
    print(f"최대 시도   : {max_attempts}")

    outcomes = run_workers(
        lambda worker_id: run_with_retry(settings, worker_id, max_attempts),
        burst_size,
    )

    print_summary(outcomes, settings)

    return 0 if all(outcome.result.is_success for outcome in outcomes) else 1


if __name__ == "__main__":
    raise SystemExit(main())
