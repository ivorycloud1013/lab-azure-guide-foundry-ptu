#!/usr/bin/env python3

import argparse
import logging
import os
import random
import time
from concurrent.futures import ThreadPoolExecutor

import openai
from openai import OpenAI

logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
log = logging.getLogger("retry")

DEFAULT_TOKEN_SCOPE = "https://ai.azure.com/.default"

AUTH_ENTRA_ID = "entra-id"
AUTH_ENTRA_ID_PREFIX = AUTH_ENTRA_ID + "="
AUTH_API_KEY_PREFIX = "api-key="
DEFAULT_IMAGE_PROMPT = "A cute baby polar bear"
DEFAULT_CHAT_PROMPT = "Explain the purpose of an API in one sentence."
IMAGE_SIZE = "1024x1024"
# --max-tokens 기본값. PTU 사용률은 prompt 토큰 + max_tokens 추정치로 계산되므로
# 실제 생성량에 가깝게 잡아야 동시 처리량이 올라간다.
MAX_OUTPUT_TOKENS = 256

API_IMAGES_GENERATE = "images.generate"
API_IMAGES_EDIT = "images.edit"
API_CHAT_COMPLETIONS = "chat.completions"
IMAGE_APIS = (API_IMAGES_GENERATE, API_IMAGES_EDIT)

# 재시도 대상 상태 코드. 그 외에는 즉시 중단한다.
# openai SDK 의 기본 재시도 대상(408/409/429/5xx)과 맞춘다.
RETRYABLE = frozenset({408, 409, 429, 500, 502, 503, 504})

# retry-after 헤더가 없을 때만 쓰는 지수 백오프.
BASE_BACKOFF_SECONDS = 1.0
BACKOFF_MULTIPLIER = 2.0
MAX_BACKOFF_SECONDS = 30.0
# 동시 요청이 같은 시각에 재차 몰리는 것을 막는 지터 비율.
JITTER_RATIO = 0.25

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
        description="PTU 의 429 를 retry-after 헤더에 맞춰 재시도한다.")
    parser.add_argument("--endpoint", required=True,
                        help="모델 배포 엔드포인트. /openai/v1/ 까지 포함한 전체 URL")
    parser.add_argument("--deployment", required=True, help="PTU 배포 이름")
    parser.add_argument("--auth", default=AUTH_ENTRA_ID, metavar="METHOD",
                        help=f"{AUTH_ENTRA_ID} (기본) | {AUTH_ENTRA_ID}=<스코프> | api-key=<키>")
    parser.add_argument("--api",
                        choices=(API_IMAGES_GENERATE, API_IMAGES_EDIT, API_CHAT_COMPLETIONS),
                        default=API_IMAGES_GENERATE,
                        help=f"호출할 API (기본 {API_IMAGES_GENERATE})")
    parser.add_argument("--prompt", help="프롬프트 (기본값은 --api 별로 다름)")
    parser.add_argument("--image", help="images.edit 의 입력 이미지 경로. images.edit 일 때 필수")
    parser.add_argument("--max-tokens", type=int, default=MAX_OUTPUT_TOKENS,
                        help=f"chat.completions 전용. PTU 사용률 추정에 직접 반영된다 "
                             f"(기본 {MAX_OUTPUT_TOKENS})")
    parser.add_argument("--max-attempts", type=int, default=5,
                        help="요청 하나당 최대 시도 횟수 (기본 5)")
    parser.add_argument("--burst", type=int, default=1,
                        help="동시 요청 수. 2 이상이면 429 를 실제로 유발할 수 있다 (기본 1)")

    args = parser.parse_args()
    args.api_key, args.token_scope = resolve_auth(args.auth, parser)
    if args.max_attempts < 1:
        parser.error("--max-attempts 는 1 이상이어야 한다")
    if args.burst < 1:
        parser.error("--burst 는 1 이상이어야 한다")
    # images.edit 는 편집 대상 이미지가 있어야 한다.
    if args.api == API_IMAGES_EDIT:
        if not args.image:
            parser.error("--api images.edit 에는 --image 로 입력 이미지를 지정해야 한다")
        if not os.path.isfile(args.image):
            parser.error(f"--image 경로에 파일이 없다: {args.image}")
    if not args.prompt:
        args.prompt = (DEFAULT_IMAGE_PROMPT if args.api in IMAGE_APIS
                       else DEFAULT_CHAT_PROMPT)
    return args


def check_endpoint(url):
    """v1 데이터플레인 경로가 아니면 404 가 난다. 고쳐주지는 않고 경고만 한다."""
    if not url.rstrip("/").endswith("/openai/v1"):
        log.warning("엔드포인트가 /openai/v1/ 로 끝나지 않는다. 404 가 날 수 있다: %s", url)
    return url


def build_client(endpoint, api_key, token_scope):
    """SDK 자동 재시도를 꺼서 재시도 동작을 이 스크립트가 직접 제어한다."""
    if api_key:
        credential = api_key
    else:
        from azure.identity import DefaultAzureCredential, get_bearer_token_provider
        credential = get_bearer_token_provider(DefaultAzureCredential(), token_scope)
    return OpenAI(base_url=endpoint, api_key=credential, max_retries=0)


def call(client, deployment, args):
    """with_raw_response 로 호출해 성공·실패 모두 원본 헤더를 얻는다."""
    if args.api == API_CHAT_COMPLETIONS:
        return client.chat.completions.with_raw_response.create(
            model=deployment,
            messages=[{"role": "user", "content": args.prompt}],
            max_completion_tokens=args.max_tokens,
        )
    if args.api == API_IMAGES_EDIT:
        with open(args.image, "rb") as source:
            return client.images.with_raw_response.edit(
                model=deployment, image=source, prompt=args.prompt, n=1, size=IMAGE_SIZE,
            )
    return client.images.with_raw_response.generate(
        model=deployment, prompt=args.prompt, n=1, size=IMAGE_SIZE,
    )


def dump_headers(title, status, headers):
    """응답 헤더를 그룹으로 나눠 출력한다. 분류에 없는 헤더는 etc 로 함께 찍는다."""
    log.info(f"\n=== {title} | HTTP {status} ===")
    lowered = {key.lower(): value for key, value in headers.items()}
    grouped = set()
    for group, names in HEADER_GROUPS.items():
        grouped.update(names)
        rows = [(name, lowered[name]) for name in names if name in lowered]
        if rows:
            log.info(f"{group}:")
            for name, value in rows:
                log.info(f"  {name}: {value}")
    others = sorted((k, v) for k, v in lowered.items() if k not in grouped)
    if others:
        log.info("etc")
        for name, value in others:
            log.info(f"  {name}: {value}")


def retry_after_seconds(headers):
    """retry-after-ms(밀리초) 를 우선 쓰고, 없으면 retry-after(초) 를 쓴다.

    Retry-After 는 HTTP-date 형식도 허용되고 중간 프록시가 그렇게 보낼 수 있다.
    숫자로 읽히지 않으면 None 을 돌려 백오프로 넘긴다.
    """
    lowered = {key.lower(): value for key, value in headers.items()}
    for name, divisor in (("retry-after-ms", 1000.0), ("retry-after", 1.0)):
        raw = lowered.get(name)
        if not raw:
            continue
        try:
            return float(raw) / divisor
        except (TypeError, ValueError):
            log.warning("%s 헤더를 숫자로 읽지 못했다: %r", name, raw)
    return None


def wait_seconds(headers, attempt_index):
    """다음 시도까지 기다릴 시간. 서비스가 알려준 값이 임의 백오프보다 정확하다."""
    header_wait = retry_after_seconds(headers)
    if header_wait is None:
        header_wait = min(BASE_BACKOFF_SECONDS * (BACKOFF_MULTIPLIER ** attempt_index),
                          MAX_BACKOFF_SECONDS)
    return header_wait + header_wait * JITTER_RATIO * random.random()


def run_worker(worker_id, client, args):
    """한 요청을 최대 --max-attempts 번까지 재시도한다."""
    for attempt_index in range(args.max_attempts):
        attempt = attempt_index + 1
        label = f"worker {worker_id} 시도 {attempt}"
        try:
            log.info(f"\n=== Request | {label} ===")
            raw = call(client, args.deployment, args)
        except openai.APIStatusError as exc:
            dump_headers(f"Response | {label}", exc.status_code, exc.response.headers)

            if exc.status_code not in RETRYABLE:
                log.error("worker %s: HTTP %s 는 재시도 대상이 아니다", worker_id, exc.status_code)
                return False
            if attempt == args.max_attempts:
                log.error("worker %s: %s 회 시도 후 포기 (HTTP %s)", worker_id, attempt, exc.status_code)
                return False

            delay = wait_seconds(exc.response.headers, attempt_index)
            source = "retry-after" if retry_after_seconds(exc.response.headers) is not None else "백오프"
            log.warning("worker %s: HTTP %s -> %.3f초 대기 (%s)",
                        worker_id, exc.status_code, delay, source)
            time.sleep(delay)
            continue
        except openai.APIConnectionError as exc:
            # 응답 자체가 없다(타임아웃 포함). retry-after 도 없으니 백오프로만 재시도한다.
            # max_retries=0 으로 SDK 기본 재시도를 껐으므로 여기서 직접 처리해야 한다.
            if attempt == args.max_attempts:
                log.error("worker %s: %s 회 시도 후 포기 (%s)",
                          worker_id, attempt, type(exc).__name__)
                return False

            delay = wait_seconds({}, attempt_index)
            log.warning("worker %s: %s -> %.3f초 대기 (백오프)",
                        worker_id, type(exc).__name__, delay)
            time.sleep(delay)
            continue

        dump_headers(f"Response | {label}", raw.http_response.status_code, raw.headers)
        log.info("worker %s: %s 번째 시도에서 성공", worker_id, attempt)
        return True

    return False


def main():
    args = parse_args()
    endpoint = check_endpoint(args.endpoint)
    log.info("PTU 배포 %s | 동시 요청 %s | 최대 시도 %s",
             args.deployment, args.burst, args.max_attempts)

    # 클라이언트는 하나만 만들어 모든 워커가 공유한다. openai 클라이언트는 스레드 안전하고,
    # 워커마다 만들면 Entra ID 자격 증명 객체가 워커 수만큼 생겨 토큰을 따로 받게 된다.
    client = build_client(endpoint, args.api_key, args.token_scope)

    if args.burst == 1:
        results = [run_worker(1, client, args)]
    else:
        with ThreadPoolExecutor(max_workers=args.burst) as pool:
            results = list(pool.map(lambda i: run_worker(i, client, args),
                                    range(1, args.burst + 1)))

    log.info("성공 %s / 전체 %s", sum(results), len(results))
    if not all(results):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
