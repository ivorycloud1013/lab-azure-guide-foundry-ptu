#!/usr/bin/env python3
"""PTU 가 429 를 돌려줄 때 Standard(PayGo) 배포로 넘기는 두 가지 방식.

client  기본값. 앱이 직접 넘긴다. PTU 가 400/429/500/503 을 돌려주면 기다리지
        않고 곧바로 표준 배포로 같은 요청을 다시 보낸다. 표준 배포가 다른
        리소스·리전에 있어도 되고 전환 조건을 앱이 통제한다.
header  x-ms-spillover-deployment 헤더로 Foundry 에 위임한다. 왕복이 한 번이라
        지연이 가장 적다. 응답에 x-ms-spillover-from-deployment 가 실려 온다.

배포 속성 spilloverDeploymentName 이 이미 설정돼 있으면 배포 설정이 우선하고
header 방식은 무시된다.

    python foundry-model-ptu-deploy-429-spillover.py \\
        --endpoint https://<resource>.openai.azure.com/openai/v1/ \\
        --ptu-deployment gpt-image-2 \\
        --standard-deployment gpt-image-2-paygo \\
        --spillover-mode header
"""

import argparse
import logging

import openai
from openai import OpenAI

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger("spillover")

DEFAULT_TOKEN_SCOPE = "https://ai.azure.com/.default"
DEFAULT_IMAGE_PROMPT = "A cute baby polar bear"
DEFAULT_CHAT_PROMPT = "Explain the purpose of an API in one sentence."

API_IMAGES_GENERATE = "images.generate"
API_CHAT_COMPLETIONS = "chat.completions"

# 서비스 측 per-request 스필오버를 요청하는 헤더.
SPILLOVER_HEADER = "x-ms-spillover-deployment"

# 스필오버를 유발하는 상태 코드. 429 는 PTU 소진, 400 은 롱컨텍스트, 500/503 은 서버 오류.
SPILLOVER_CODES = frozenset({400, 429, 500, 503})

HEADER_GROUPS = {
    "throttling": (
        "retry-after", "retry-after-ms",
        "x-ratelimit-limit-requests", "x-ratelimit-limit-tokens",
        "x-ratelimit-remaining-requests", "x-ratelimit-remaining-tokens",
        "x-ratelimit-reset-requests", "x-ratelimit-reset-tokens",
    ),
    "spillover": (
        "x-ms-deployment-name",
        "x-ms-spillover-from-deployment",
        "x-ms-spillover-error",
    ),
    "trace": (
        "apim-request-id", "x-request-id", "x-ms-request-id", "x-ms-client-request-id",
        "x-ms-region", "azureml-model-session", "openai-processing-ms", "openai-model",
        "x-envoy-upstream-service-time",
    ),
}


def parse_args():
    parser = argparse.ArgumentParser(
        description="PTU 429 를 Standard 배포로 넘기는 두 방식을 비교한다.")
    parser.add_argument("--endpoint", required=True,
                        help="모델 배포 엔드포인트. /openai/v1/ 까지 포함한 전체 URL")
    parser.add_argument("--ptu-deployment", required=True, help="PTU 배포 이름")
    parser.add_argument("--standard-deployment", required=True,
                        help="스필오버 대상 Standard(PayGo) 배포 이름")
    parser.add_argument("--standard-endpoint",
                        help="표준 배포가 다른 리소스에 있을 때만 지정 (기본: --endpoint 와 동일)")
    parser.add_argument("--spillover-mode", choices=("client", "header", "both"), default="client",
                        help="client=앱이 직접 전환, header=서비스에 위임, both=둘 다 (기본 client)")
    parser.add_argument("--api-key",
                        help="지정하면 키 인증. 생략하면 Entra ID (권장). "
                             "커맨드라인의 키는 프로세스 목록에 노출된다")
    parser.add_argument("--token-scope", default=DEFAULT_TOKEN_SCOPE,
                        help=f"Entra ID 토큰 스코프 (기본 {DEFAULT_TOKEN_SCOPE})")
    parser.add_argument("--api",
                        choices=(API_IMAGES_GENERATE, API_CHAT_COMPLETIONS),
                        default=API_IMAGES_GENERATE,
                        help=f"호출할 API (기본 {API_IMAGES_GENERATE})")
    parser.add_argument("--prompt", help="프롬프트 (기본값은 --api 별로 다름)")
    parser.add_argument("--image-size", default="1024x1024",
                        help="images.generate 전용 (기본 1024x1024)")
    parser.add_argument("--max-tokens", type=int, default=256,
                        help="chat.completions 전용. PTU 사용률 추정에 직접 반영된다 (기본 256)")

    args = parser.parse_args()
    if not args.prompt:
        args.prompt = (DEFAULT_IMAGE_PROMPT if args.api == API_IMAGES_GENERATE
                       else DEFAULT_CHAT_PROMPT)
    return args


def check_endpoint(url):
    """v1 데이터플레인 경로가 아니면 404 가 난다. 고쳐주지는 않고 경고만 한다."""
    if not url.rstrip("/").endswith("/openai/v1"):
        log.warning("엔드포인트가 /openai/v1/ 로 끝나지 않는다. 404 가 날 수 있다: %s", url)
    return url


def build_client(endpoint, api_key, token_scope):
    """SDK 자동 재시도를 꺼서 전환 시점을 이 스크립트가 직접 제어한다."""
    if api_key:
        credential = api_key
    else:
        from azure.identity import DefaultAzureCredential, get_bearer_token_provider
        credential = get_bearer_token_provider(DefaultAzureCredential(), token_scope)
    return OpenAI(base_url=endpoint, api_key=credential, max_retries=0)


def call(client, deployment, args, extra_headers=None):
    """with_raw_response 로 호출해 성공·실패 모두 원본 헤더를 얻는다."""
    if args.api == API_CHAT_COMPLETIONS:
        return client.chat.completions.with_raw_response.create(
            model=deployment,
            messages=[{"role": "user", "content": args.prompt}],
            max_completion_tokens=args.max_tokens,
            extra_headers=extra_headers,
        )
    return client.images.with_raw_response.generate(
        model=deployment, prompt=args.prompt, n=1, size=args.image_size,
        extra_headers=extra_headers,
    )


def dump_headers(title, status, headers):
    """응답 헤더를 그룹으로 나눠 출력한다. 분류에 없는 헤더는 etc 로 함께 찍는다."""
    print(f"\n=== {title} | HTTP {status} ===")
    lowered = {key.lower(): value for key, value in headers.items()}
    grouped = set()
    for group, names in HEADER_GROUPS.items():
        grouped.update(names)
        rows = [(name, lowered[name]) for name in names if name in lowered]
        if rows:
            print(f"[{group}]")
            for name, value in rows:
                print(f"  {name}: {value}")
    others = sorted((k, v) for k, v in lowered.items() if k not in grouped)
    if others:
        print("[etc]")
        for name, value in others:
            print(f"  {name}: {value}")


def report_spillover(headers):
    """서비스 측 스필오버가 일어났는지 헤더 세 개로 판정한다."""
    lowered = {key.lower(): value for key, value in headers.items()}
    spilled_from = lowered.get("x-ms-spillover-from-deployment")
    if spilled_from:
        log.info("서비스 측 스필오버 발생: %s -> %s (PTU 원본 코드 %s)",
                 spilled_from, lowered.get("x-ms-deployment-name"),
                 lowered.get("x-ms-spillover-error"))
    else:
        log.info("스필오버 없음. %s 가 직접 처리했다", lowered.get("x-ms-deployment-name"))


def run_client_spillover(ptu_endpoint, standard_endpoint, args):
    """PTU 를 호출하고 실패하면 앱이 직접 표준 배포로 넘긴다."""
    ptu_client = build_client(ptu_endpoint, args.api_key, args.token_scope)

    try:
        raw = call(ptu_client, args.ptu_deployment, args)
    except openai.APIStatusError as exc:
        dump_headers("1차 PTU", exc.status_code, exc.response.headers)

        if exc.status_code not in SPILLOVER_CODES:
            log.error("HTTP %s 는 스필오버 대상이 아니다", exc.status_code)
            return False

        log.warning("PTU 가 HTTP %s -> 대기 없이 %s 로 전환",
                    exc.status_code, args.standard_deployment)
        standard_client = build_client(standard_endpoint, args.api_key, args.token_scope)
        try:
            fallback = call(standard_client, args.standard_deployment, args)
        except openai.APIStatusError as fallback_exc:
            dump_headers("2차 Standard", fallback_exc.status_code, fallback_exc.response.headers)
            log.error("표준 배포도 실패: HTTP %s", fallback_exc.status_code)
            return False

        dump_headers("2차 Standard", fallback.http_response.status_code, fallback.headers)
        log.info("표준 배포 %s 가 처리 완료", args.standard_deployment)
        return True

    dump_headers("1차 PTU", raw.http_response.status_code, raw.headers)
    log.info("PTU 가 정상 처리 -> 스필오버 불필요")
    return True


def run_header_spillover(ptu_endpoint, args):
    """x-ms-spillover-deployment 헤더를 붙여 서비스가 넘기게 한다."""
    client = build_client(ptu_endpoint, args.api_key, args.token_scope)
    title = f"{SPILLOVER_HEADER}: {args.standard_deployment}"

    try:
        raw = call(client, args.ptu_deployment, args,
                   extra_headers={SPILLOVER_HEADER: args.standard_deployment})
    except openai.APIStatusError as exc:
        dump_headers(title, exc.status_code, exc.response.headers)
        log.error("스필오버 후에도 실패: HTTP %s", exc.status_code)
        return False

    dump_headers(title, raw.http_response.status_code, raw.headers)
    report_spillover(raw.headers)
    return True


def main():
    args = parse_args()
    ptu_endpoint = check_endpoint(args.endpoint)
    standard_endpoint = check_endpoint(args.standard_endpoint) if args.standard_endpoint else ptu_endpoint

    log.info("PTU %s -> Standard %s | 방식 %s",
             args.ptu_deployment, args.standard_deployment, args.spillover_mode)

    results = []
    if args.spillover_mode in ("client", "both"):
        results.append(run_client_spillover(ptu_endpoint, standard_endpoint, args))
    if args.spillover_mode in ("header", "both"):
        results.append(run_header_spillover(ptu_endpoint, args))

    if not all(results):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
