#!/usr/bin/env python3
"""PTU 가 429 를 돌려줄 때 표준(PayGo) 배포로 넘기는 두 가지 방식을 보여준다.

방식 1 — client (기본값)
    클라이언트가 직접 넘긴다. PTU 배포를 호출해 400/429/500/503 을 받으면
    기다리지 않고 곧바로 표준 배포로 같은 요청을 다시 보낸다. 표준 배포가
    다른 리소스/리전에 있어도 되고, 전환 여부와 조건을 애플리케이션이 전부
    통제한다는 게 장점이다.

방식 2 — header
    서비스가 대신 넘긴다. 요청에 ``x-ms-spillover-deployment`` 헤더를 붙이면
    Foundry 가 PTU 실패를 감지해 같은 리소스 안의 표준 배포로 라우팅한다.
    왕복이 한 번뿐이라 지연이 가장 적다. 응답에는 ``x-ms-spillover-from-deployment``
    와 ``x-ms-spillover-error`` 가 실려 온다.

    주의: 배포 속성 spilloverDeploymentName 이 이미 설정돼 있으면 배포 설정이
    우선하며 이 헤더는 무시된다.

두 방식 모두 매 호출의 응답 헤더를 전부 출력한다.

사용법:
    python foundry-ptu-429-spillover.py
    FOUNDRY_SPILLOVER_MODE=header python foundry-ptu-429-spillover.py
    FOUNDRY_SPILLOVER_MODE=both FOUNDRY_BURST=20 python foundry-ptu-429-spillover.py
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from foundry_ptu_common import (
    PRINT_LOCK,
    SPILLOVER_STATUS_CODES,
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
    run_workers,
)

# ---------------------------------------------------------------------------
# 상수
# ---------------------------------------------------------------------------

#: 서비스 측 per-request 스필오버를 요청하는 헤더 이름.
SPILLOVER_REQUEST_HEADER = "x-ms-spillover-deployment"

MODE_CLIENT = "client"
MODE_HEADER = "header"
MODE_BOTH = "both"
SUPPORTED_SPILLOVER_MODES = (MODE_CLIENT, MODE_HEADER, MODE_BOTH)

DEFAULT_BURST_SIZE = 1


@dataclass(frozen=True)
class SpilloverOutcome:
    """워커 하나가 수행한 스필오버 시나리오 결과."""

    worker_id: int
    mode: str
    primary: CallResult
    fallback: CallResult | None

    @property
    def final(self) -> CallResult:
        """최종적으로 사용자에게 돌려줄 응답."""
        return self.fallback if self.fallback is not None else self.primary

    @property
    def did_fall_back(self) -> bool:
        return self.fallback is not None


def _resolve_spillover_mode() -> str:
    mode = os.environ.get("FOUNDRY_SPILLOVER_MODE", MODE_CLIENT).strip().lower()
    if mode not in SUPPORTED_SPILLOVER_MODES:
        raise ConfigError(
            f"FOUNDRY_SPILLOVER_MODE 는 {SUPPORTED_SPILLOVER_MODES} 중 하나여야 합니다 "
            f"(받은 값: {mode!r})."
        )
    return mode


# ---------------------------------------------------------------------------
# 방식 1: 클라이언트 측 스필오버
# ---------------------------------------------------------------------------


def run_client_spillover(settings: Settings, worker_id: int) -> SpilloverOutcome:
    """PTU 를 호출하고, 실패하면 클라이언트가 표준 배포로 직접 넘긴다."""
    assert settings.standard is not None  # load_settings(require_standard=True) 가 보장

    ptu_client = build_client(settings, settings.ptu)
    primary = call_deployment(ptu_client, settings, settings.ptu)

    with PRINT_LOCK:
        print_response_headers(primary, title=f"[client] worker {worker_id} / 1차: PTU")
        print_spillover_verdict(primary)

    if primary.is_success:
        with PRINT_LOCK:
            print(f"\n[client][worker {worker_id}] PTU 가 정상 처리 → 스필오버 불필요")
        return SpilloverOutcome(worker_id, MODE_CLIENT, primary, None)

    if primary.status_code not in SPILLOVER_STATUS_CODES:
        with PRINT_LOCK:
            print(
                f"\n[client][worker {worker_id}] 상태 코드 {primary.status_code} 는 "
                f"스필오버 대상이 아닙니다 → 그대로 실패 처리"
            )
        return SpilloverOutcome(worker_id, MODE_CLIENT, primary, None)

    with PRINT_LOCK:
        print(
            f"\n[client][worker {worker_id}] PTU 가 {primary.status_code} → 대기 없이 "
            f"{settings.standard.deployment} 로 전환"
        )

    standard_client = build_client(settings, settings.standard)
    fallback = call_deployment(standard_client, settings, settings.standard)

    with PRINT_LOCK:
        print_response_headers(
            fallback, title=f"[client] worker {worker_id} / 2차: Standard(PayGo)"
        )
        print_spillover_verdict(fallback)

    return SpilloverOutcome(worker_id, MODE_CLIENT, primary, fallback)


# ---------------------------------------------------------------------------
# 방식 2: 서비스 측 per-request 스필오버
# ---------------------------------------------------------------------------


def run_header_spillover(settings: Settings, worker_id: int) -> SpilloverOutcome:
    """x-ms-spillover-deployment 헤더를 붙여 서비스가 넘기게 한다."""
    assert settings.standard is not None

    client = build_client(settings, settings.ptu)
    result = call_deployment(
        client,
        settings,
        settings.ptu,
        extra_headers={SPILLOVER_REQUEST_HEADER: settings.standard.deployment},
    )

    with PRINT_LOCK:
        print_response_headers(
            result,
            title=f"[header] worker {worker_id} / {SPILLOVER_REQUEST_HEADER}: "
                  f"{settings.standard.deployment}",
        )
        print_spillover_verdict(result)

    return SpilloverOutcome(worker_id, MODE_HEADER, result, None)


# ---------------------------------------------------------------------------
# 실행 / 요약
# ---------------------------------------------------------------------------


def print_summary(outcomes: list[SpilloverOutcome], settings: Settings) -> None:
    print("\n" + "=" * 78)
    print("요약")
    print("=" * 78)

    for outcome in sorted(outcomes, key=lambda item: (item.mode, item.worker_id)):
        final = outcome.final
        verdict = "성공" if final.is_success else f"실패({final.status_code})"

        if outcome.mode == MODE_CLIENT:
            route = (
                f"PTU({outcome.primary.status_code}) → {final.target.deployment}"
                if outcome.did_fall_back
                else f"PTU({outcome.primary.status_code}) 직접 처리"
            )
        else:
            served = final.headers.get("x-ms-deployment-name") or "(헤더 없음)"
            spilled = final.headers.get("x-ms-spillover-from-deployment")
            route = f"서비스 라우팅 → {served}" + (f" (spilled from {spilled})" if spilled else "")

        body = describe_payload(final, settings) if final.is_success else ""
        print(f"  [{outcome.mode}] worker {outcome.worker_id:>3} | {verdict:<10} | {route} | {body}")

    succeeded = sum(1 for outcome in outcomes if outcome.final.is_success)
    print(f"\n  성공 {succeeded} / 전체 {len(outcomes)}")


def main() -> int:
    try:
        # 두 방식 모두 표준 배포 이름이 있어야 의미가 있으므로 필수로 강제한다.
        settings = load_settings(require_standard=True)
        spillover_mode = _resolve_spillover_mode()
        burst_size = positive_int_env("FOUNDRY_BURST", DEFAULT_BURST_SIZE)
    except ConfigError as exc:
        fail(str(exc))
        return 1

    assert settings.standard is not None

    print(f"mode          : {settings.mode}")
    print(f"스필오버 방식 : {spillover_mode}")
    print(f"PTU 배포      : {settings.ptu.deployment} ({settings.ptu.endpoint})")
    print(f"표준 배포     : {settings.standard.deployment} ({settings.standard.endpoint})")
    print(f"동시 요청     : {burst_size}")

    outcomes: list[SpilloverOutcome] = []

    if spillover_mode in (MODE_CLIENT, MODE_BOTH):
        outcomes.extend(
            run_workers(lambda worker_id: run_client_spillover(settings, worker_id), burst_size)
        )

    if spillover_mode in (MODE_HEADER, MODE_BOTH):
        outcomes.extend(
            run_workers(lambda worker_id: run_header_spillover(settings, worker_id), burst_size)
        )

    print_summary(outcomes, settings)

    return 0 if all(outcome.final.is_success for outcome in outcomes) else 1


if __name__ == "__main__":
    raise SystemExit(main())
