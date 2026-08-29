#!/usr/bin/env python3
"""PTU 배포에 기본 호출을 한 번 보내고 응답 헤더를 전부 까본다.

확인 목적
1. Entra ID 인증과 v1 엔드포인트 설정이 맞는지.
2. 요청을 실제로 처리한 배포가 어디인지 (``x-ms-deployment-name``).
3. 모델 배포 자체에 걸어둔 서비스 측 스필오버(``spilloverDeploymentName``)가
   동작했는지 (``x-ms-spillover-from-deployment`` / ``x-ms-spillover-error``).
4. 스로틀링 관련 헤더(``retry-after``, ``retry-after-ms``, ``x-ratelimit-*``)의 현재 값.

이 스크립트는 재시도도 스필오버도 직접 하지 않는다. 서비스가 무엇을 돌려주는지
있는 그대로 보기 위한 기준선(baseline)이다.

사용법:
    python foundry-ptu-basic.py
"""

from __future__ import annotations

from foundry_ptu_common import (
    ConfigError,
    build_client,
    call_deployment,
    describe_payload,
    fail,
    load_settings,
    print_response_headers,
    print_spillover_verdict,
    retry_after_seconds,
)

TITLE = "기본 호출 (재시도/스필오버 없음)"


def main() -> int:
    try:
        settings = load_settings()
    except ConfigError as exc:
        fail(str(exc))
        return 1  # fail() 이 종료하므로 도달하지 않는다.

    print(f"mode        : {settings.mode}")
    print(f"prompt      : {settings.prompt!r}")
    print(f"auth        : {'API key' if settings.api_key else 'Entra ID (' + settings.token_scope + ')'}")

    client = build_client(settings, settings.ptu)
    result = call_deployment(client, settings, settings.ptu)

    print_response_headers(result, title=TITLE)
    print_spillover_verdict(result)

    if result.is_success:
        print(f"\n[본문] {describe_payload(result, settings)}")
        return 0

    print(f"\n[실패] {type(result.error).__name__}: {result.error}")

    wait_seconds = retry_after_seconds(result.headers)
    if wait_seconds is not None:
        print(f"[힌트] 서비스가 {wait_seconds:.3f}초 후 재시도를 권장합니다.")
        print("       재시도 구현은 foundry-ptu-429-retry.py 를 참고하세요.")
    if result.status_code == 429:
        print("[힌트] PTU 사용률이 100% 에 도달했습니다. 서비스 오류가 아니라 트래픽 관리 신호입니다.")

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
