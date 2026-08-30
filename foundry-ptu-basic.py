#!/usr/bin/env python3
"""PTU 배포에 한 번 호출하고 응답 헤더를 전부 출력한다.

재시도도 spillover 도 하지 않는다. 인증과 엔드포인트 설정이 맞는지,
어느 배포가 요청을 처리했는지 확인하는 기준선이다.

    python foundry-ptu-basic.py \\
        --endpoint https://<resource>.openai.azure.com \\
        --ptu-deployment gpt-image-2
"""

import argparse
import logging

import openai
from openai import OpenAI

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger("basic")

DEFAULT_TOKEN_SCOPE = "https://ai.azure.com/.default"
DEFAULT_IMAGE_PROMPT = "A cute baby polar bear"
DEFAULT_CHAT_PROMPT = "Explain the purpose of an API in one sentence."

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
        description="PTU 배포를 한 번 호출하고 응답 헤더를 출력한다.")
    parser.add_argument("--endpoint", required=True,
                        help="Foundry 리소스 엔드포인트. /openai/v1/ 는 자동으로 붙인다")
    parser.add_argument("--ptu-deployment", required=True, help="PTU 배포 이름")
    parser.add_argument("--api-key",
                        help="지정하면 키 인증. 생략하면 Entra ID (권장). "
                             "커맨드라인의 키는 프로세스 목록에 노출된다")
    parser.add_argument("--token-scope", default=DEFAULT_TOKEN_SCOPE,
                        help=f"Entra ID 토큰 스코프 (기본 {DEFAULT_TOKEN_SCOPE})")
    parser.add_argument("--mode", choices=("image", "chat"), default="image",
                        help="호출할 API 종류 (기본 image)")
    parser.add_argument("--prompt", help="프롬프트 (기본값은 mode 별로 다름)")
    parser.add_argument("--image-size", default="1024x1024", help="image 모드 전용 (기본 1024x1024)")
    parser.add_argument("--max-tokens", type=int, default=256,
                        help="chat 모드 전용. PTU 사용률 추정에 직접 반영된다 (기본 256)")

    args = parser.parse_args()
    if not args.prompt:
        args.prompt = DEFAULT_IMAGE_PROMPT if args.mode == "image" else DEFAULT_CHAT_PROMPT
    return args


def base_url(raw):
    """엔드포인트는 /openai/v1/ 로 끝나야 한다. 아니면 404 가 난다."""
    url = raw.rstrip("/")
    return url + "/" if url.endswith("/openai/v1") else url + "/openai/v1/"


def build_client(endpoint, api_key, token_scope):
    """키가 있으면 키 인증, 없으면 Entra ID 토큰 프로바이더를 쓴다.

    SDK 자동 재시도는 끈다. 매 시도의 응답 헤더를 직접 보기 위해서다.
    """
    if api_key:
        credential = api_key
    else:
        from azure.identity import DefaultAzureCredential, get_bearer_token_provider
        credential = get_bearer_token_provider(DefaultAzureCredential(), token_scope)
    return OpenAI(base_url=endpoint, api_key=credential, max_retries=0)


def call(client, deployment, args):
    """with_raw_response 로 호출해 성공·실패 모두 원본 헤더를 얻는다."""
    if args.mode == "chat":
        return client.chat.completions.with_raw_response.create(
            model=deployment,
            messages=[{"role": "user", "content": args.prompt}],
            max_completion_tokens=args.max_tokens,
        )
    return client.images.with_raw_response.generate(
        model=deployment, prompt=args.prompt, n=1, size=args.image_size,
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
    served = lowered.get("x-ms-deployment-name")
    spilled_from = lowered.get("x-ms-spillover-from-deployment")

    if spilled_from:
        log.info("서비스 측 스필오버 발생: %s -> %s (PTU 원본 코드 %s)",
                 spilled_from, served, lowered.get("x-ms-spillover-error"))
    elif served:
        log.info("스필오버 없음. %s 가 직접 처리했다", served)
    else:
        log.info("스필오버 헤더가 없다. 배포에 spilloverDeploymentName 이 없거나 PTU 에 여유가 있다")


def main():
    args = parse_args()
    client = build_client(base_url(args.endpoint), args.api_key, args.token_scope)
    log.info("PTU 배포 %s 호출 (mode=%s)", args.ptu_deployment, args.mode)

    try:
        raw = call(client, args.ptu_deployment, args)
    except openai.APIStatusError as exc:
        dump_headers("PTU 응답", exc.status_code, exc.response.headers)
        log.error("호출 실패: HTTP %s", exc.status_code)
        if exc.status_code == 429:
            log.error("PTU 사용률이 100% 에 도달했다. 재시도는 foundry-ptu-429-retry.py 참고")
        raise SystemExit(1)

    dump_headers("PTU 응답", raw.http_response.status_code, raw.headers)
    report_spillover(raw.headers)
    log.info("성공")


if __name__ == "__main__":
    main()
