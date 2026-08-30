#!/usr/bin/env python3
"""PTU 가 429 를 돌려줄 때 Standard(PayGo) 배포로 넘기는 두 가지 방식.

client  앱이 직접 넘긴다. PTU 가 200 이 아닌 응답을 돌려주면 상태 코드를
        가리지 않고 곧바로 표준 배포로 같은 요청을 다시 보낸다.
header  x-ms-spillover-deployment 헤더로 Foundry 에 위임한다. 왕복이 한 번이라
        지연이 가장 적다. 응답에 x-ms-spillover-from-deployment 가 실려 온다.

배포 속성 spilloverDeploymentName 이 이미 설정돼 있으면 배포 설정이 우선하고
header 방식은 무시된다.

    python foundry-model-ptu-deploy-429-spillover.py \\
        --endpoint https://<foundry-resource-subdomain>.openai.azure.com/openai/v1/ \\
        --deployment gpt-image-2 \\
        --spillover-deployment gpt-image-2-paygo \\
        --spillover-mode header
"""

import argparse
import logging

import openai
from openai import OpenAI

logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
log = logging.getLogger("spillover")

DEFAULT_TOKEN_SCOPE = "https://ai.azure.com/.default"

AUTH_ENTRA_ID = "entra-id"
AUTH_ENTRA_ID_PREFIX = AUTH_ENTRA_ID + "="
AUTH_API_KEY_PREFIX = "api-key="
DEFAULT_IMAGE_PROMPT = "A cute baby polar bear"
DEFAULT_CHAT_PROMPT = "Explain the purpose of an API in one sentence."
IMAGE_SIZE = "1024x1024"
# PTU 사용률은 prompt 토큰 + max_tokens 추정치로 계산된다.
# 실제 생성량에 가깝게 잡아야 동시 처리량이 올라간다.
MAX_OUTPUT_TOKENS = 256

API_IMAGES_GENERATE = "images.generate"
API_CHAT_COMPLETIONS = "chat.completions"

# 서비스 측 per-request 스필오버를 요청하는 헤더.
SPILLOVER_HEADER = "x-ms-spillover-deployment"


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


def resolve_auth(value, parser):
    """--auth 값을 (api_key, token_scope) 로 푼다. Entra ID 면 api_key 는 None.

    entra-id            기본 스코프로 Entra ID 인증 (기본값)
    entra-id=<스코프>    스코프를 지정해 Entra ID 인증
    api-key=<키>        키로 인증
    """
    if value == AUTH_ENTRA_ID:
        return None, DEFAULT_TOKEN_SCOPE
    if value.startswith(AUTH_ENTRA_ID_PREFIX):
        scope = value[len(AUTH_ENTRA_ID_PREFIX):]
        if not scope:
            parser.error(f"--auth {AUTH_ENTRA_ID_PREFIX} 뒤에 토큰 스코프를 지정해야 한다")
        return None, scope
    if value.startswith(AUTH_API_KEY_PREFIX):
        key = value[len(AUTH_API_KEY_PREFIX):]
        if not key:
            parser.error(f"--auth {AUTH_API_KEY_PREFIX} 뒤에 키를 지정해야 한다")
        return key, DEFAULT_TOKEN_SCOPE
    parser.error(f"--auth 는 {AUTH_ENTRA_ID}, {AUTH_ENTRA_ID_PREFIX}<스코프>, "
                 f"{AUTH_API_KEY_PREFIX}<키> 중 하나여야 한다")


def parse_args():
    parser = argparse.ArgumentParser(
        description="PTU 429 를 Standard 배포로 넘기는 두 방식을 비교한다.")
    parser.add_argument("--endpoint", required=True,
                        help="모델 배포 엔드포인트. /openai/v1/ 까지 포함한 전체 URL")
    parser.add_argument("--deployment", required=True, help="PTU 배포 이름")
    parser.add_argument("--spillover-deployment",
                        help="spillover 대상 Standard(PayGo) 배포 이름. --spillover-mode 를 줄 때 필수")
    parser.add_argument("--spillover-mode", choices=("client", "header"),
                        help="미지정=배포 속성 spilloverDeploymentName 에 맡기고 그대로 호출, "
                             "header=요청 헤더로 서비스에 위임, client=클라이언트가 직접 전환")
    parser.add_argument("--auth", default=AUTH_ENTRA_ID, metavar="METHOD",
                        help=f"{AUTH_ENTRA_ID} (기본) | {AUTH_ENTRA_ID}=<스코프> | api-key=<키>")
    parser.add_argument("--api",
                        choices=(API_IMAGES_GENERATE, API_CHAT_COMPLETIONS),
                        default=API_IMAGES_GENERATE,
                        help=f"호출할 API (기본 {API_IMAGES_GENERATE})")
    parser.add_argument("--prompt", help="프롬프트 (기본값은 --api 별로 다름)")

    args = parser.parse_args()
    args.api_key, args.token_scope = resolve_auth(args.auth, parser)
    if args.spillover_mode and not args.spillover_deployment:
        parser.error(f"--spillover-mode {args.spillover_mode} 에는 --spillover-deployment 가 필요하다")
    if args.spillover_deployment and not args.spillover_mode:
        log.warning("--spillover-mode 가 없어 --spillover-deployment %s 는 쓰이지 않는다. "
                    "배포 속성 spilloverDeploymentName 에 맡긴다", args.spillover_deployment)
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
            max_completion_tokens=MAX_OUTPUT_TOKENS,
            extra_headers=extra_headers,
        )
    return client.images.with_raw_response.generate(
        model=deployment, prompt=args.prompt, n=1, size=IMAGE_SIZE,
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
            print(f"{group}:")
            for name, value in rows:
                print(f"  {name}: {value}")
    others = sorted((k, v) for k, v in lowered.items() if k not in grouped)
    if others:
        print("etc")
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
        log.info("스필오버 없음")


def run_standard_fallback(client, args):
    """표준 배포로 같은 요청을 다시 보낸다. 클라이언트는 그대로 재사용한다."""
    try:
        print("\n=== Request | 2차 Standard ===")
        fallback = call(client, args.spillover_deployment, args)
    except openai.APIStatusError as exc:
        dump_headers("Response | 2차 Standard", exc.status_code, exc.response.headers)
        log.error("표준 배포도 실패: HTTP %s", exc.status_code)
        return False
    except openai.APIConnectionError as exc:
        log.error("표준 배포 연결 실패: %s", type(exc).__name__)
        return False

    dump_headers("Response | 2차 Standard", fallback.http_response.status_code, fallback.headers)
    log.info("표준 배포 %s 가 처리 완료", args.spillover_deployment)
    return True


def run_client_spillover(endpoint, args):
    """PTU 를 호출하고 실패하면 앱이 직접 표준 배포로 넘긴다.

    엔드포인트도 인증도 같으므로 클라이언트를 새로 만들지 않고 재사용한다.
    새로 만들면 자격 증명도 새로 만들어져 전환 경로에 토큰 취득이 끼어든다.
    """
    client = build_client(endpoint, args.api_key, args.token_scope)

    try:
        print("\n=== Request | 1차 PTU ===")
        raw = call(client, args.deployment, args)
    except openai.APIStatusError as exc:
        dump_headers("Response | 1차 PTU", exc.status_code, exc.response.headers)

        # 상태 코드를 가리지 않는다. 200 이 아니면 곧바로 넘긴다.
        log.warning("PTU 가 HTTP %s -> 대기 없이 %s 로 전환",
                    exc.status_code, args.spillover_deployment)
        return run_standard_fallback(client, args)
    except openai.APIConnectionError as exc:
        # 응답이 아예 오지 않은 경우(연결 실패·타임아웃)도 전환 대상이다.
        log.warning("PTU 연결 실패 (%s) -> 대기 없이 %s 로 전환",
                    type(exc).__name__, args.spillover_deployment)
        return run_standard_fallback(client, args)

    dump_headers("Response | 1차 PTU", raw.http_response.status_code, raw.headers)
    log.info("PTU 가 정상 처리 -> 스필오버 불필요")
    return True


def run_deployment_spillover(ptu_endpoint, args):
    """배포 속성 spilloverDeploymentName 에 맡긴다.

    클라이언트는 헤더도 붙이지 않고 재요청도 하지 않는다. 전환이 일어났는지는
    응답 헤더로만 확인한다.
    """
    client = build_client(ptu_endpoint, args.api_key, args.token_scope)
    try:
        print("\n=== Request | PTU ===")
        raw = call(client, args.deployment, args)
    except openai.APIStatusError as exc:
        dump_headers("Response | PTU", exc.status_code, exc.response.headers)
        log.error("호출 실패: HTTP %s", exc.status_code)
        if exc.status_code == 429:
            log.error("배포에 spilloverDeploymentName 이 없거나 Standard 배포도 처리하지 못했다")
        return False
    except openai.APIConnectionError as exc:
        log.error("PTU 연결 실패: %s", type(exc).__name__)
        return False

    dump_headers("Response | PTU", raw.http_response.status_code, raw.headers)
    report_spillover(raw.headers)
    return True


def run_header_spillover(ptu_endpoint, args):
    """x-ms-spillover-deployment 헤더를 붙여 서비스가 넘기게 한다."""
    client = build_client(ptu_endpoint, args.api_key, args.token_scope)
    label = f"{SPILLOVER_HEADER}: {args.spillover_deployment}"

    try:
        print(f"\n=== Request | {label} ===")
        raw = call(client, args.deployment, args,
                   extra_headers={SPILLOVER_HEADER: args.spillover_deployment})
    except openai.APIStatusError as exc:
        dump_headers(f"Response | {label}", exc.status_code, exc.response.headers)
        log.error("스필오버 후에도 실패: HTTP %s", exc.status_code)
        return False
    except openai.APIConnectionError as exc:
        log.error("연결 실패: %s", type(exc).__name__)
        return False

    dump_headers(f"Response | {label}", raw.http_response.status_code, raw.headers)
    report_spillover(raw.headers)
    return True


def main():
    args = parse_args()
    ptu_endpoint = check_endpoint(args.endpoint)

    log.info("PTU %s -> Standard %s | 방식 %s",
             args.deployment, args.spillover_deployment or "(배포 속성)",
             args.spillover_mode or "미지정")

    if args.spillover_mode == "client":
        succeeded = run_client_spillover(ptu_endpoint, args)
    elif args.spillover_mode == "header":
        succeeded = run_header_spillover(ptu_endpoint, args)
    else:
        succeeded = run_deployment_spillover(ptu_endpoint, args)

    if not succeeded:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
