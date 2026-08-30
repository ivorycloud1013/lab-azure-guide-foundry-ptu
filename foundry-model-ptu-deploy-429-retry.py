#!/usr/bin/env python3
"""PTU 가 돌려준 429 를 retry-after 헤더에 맞춰 재시도한다.

PTU 는 사용률이 100% 에 닿으면 큐잉하지 않고 즉시 429 를 돌려주며,
retry-after-ms / retry-after 로 다시 올 시점을 알려준다. 그 값을 우선 쓰고,
헤더가 없을 때만 지수 백오프로 폴백한다.

    python foundry-model-ptu-deploy-429-retry.py \\
        --endpoint https://<resource>.openai.azure.com \\
        --ptu-deployment gpt-image-2 \\
        --burst 20 --max-attempts 6
"""

import argparse
import logging
import random
import time
from concurrent.futures import ThreadPoolExecutor

import openai
from openai import OpenAI

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger("retry")

DEFAULT_TOKEN_SCOPE = "https://ai.azure.com/.default"
DEFAULT_IMAGE_PROMPT = "A cute baby polar bear"
DEFAULT_CHAT_PROMPT = "Explain the purpose of an API in one sentence."

API_IMAGES_GENERATE = "images.generate"
API_CHAT_COMPLETIONS = "chat.completions"

# 재시도 대상 상태 코드. 그 외에는 즉시 중단한다.
RETRYABLE = frozenset({408, 429, 500, 502, 503, 504})

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


def parse_args():
    parser = argparse.ArgumentParser(
        description="PTU 의 429 를 retry-after 헤더에 맞춰 재시도한다.")
    parser.add_argument("--endpoint", required=True,
                        help="Foundry 리소스 엔드포인트. /openai/v1/ 는 자동으로 붙인다")
    parser.add_argument("--ptu-deployment", required=True, help="PTU 배포 이름")
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
    parser.add_argument("--max-attempts", type=int, default=5,
                        help="요청 하나당 최대 시도 횟수 (기본 5)")
    parser.add_argument("--burst", type=int, default=1,
                        help="동시 요청 수. 2 이상이면 429 를 실제로 유발할 수 있다 (기본 1)")

    args = parser.parse_args()
    if not args.prompt:
        args.prompt = (DEFAULT_IMAGE_PROMPT if args.api == API_IMAGES_GENERATE
                       else DEFAULT_CHAT_PROMPT)
    return args


def base_url(raw):
    """엔드포인트는 /openai/v1/ 로 끝나야 한다. 아니면 404 가 난다."""
    url = raw.rstrip("/")
    return url + "/" if url.endswith("/openai/v1") else url + "/openai/v1/"


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


def retry_after_seconds(headers):
    """retry-after-ms(밀리초) 를 우선 쓰고, 없으면 retry-after(초) 를 쓴다."""
    lowered = {key.lower(): value for key, value in headers.items()}
    raw_ms = lowered.get("retry-after-ms")
    if raw_ms:
        return int(raw_ms) / 1000.0
    raw_seconds = lowered.get("retry-after")
    if raw_seconds:
        return float(raw_seconds)
    return None


def wait_seconds(headers, attempt_index):
    """다음 시도까지 기다릴 시간. 서비스가 알려준 값이 임의 백오프보다 정확하다."""
    header_wait = retry_after_seconds(headers)
    if header_wait is None:
        header_wait = min(BASE_BACKOFF_SECONDS * (BACKOFF_MULTIPLIER ** attempt_index),
                          MAX_BACKOFF_SECONDS)
    return header_wait + header_wait * JITTER_RATIO * random.random()


def run_worker(worker_id, endpoint, args):
    """한 요청을 최대 --max-attempts 번까지 재시도한다."""
    client = build_client(endpoint, args.api_key, args.token_scope)

    for attempt_index in range(args.max_attempts):
        attempt = attempt_index + 1
        try:
            raw = call(client, args.ptu_deployment, args)
        except openai.APIStatusError as exc:
            dump_headers(f"worker {worker_id} 시도 {attempt}", exc.status_code, exc.response.headers)

            if exc.status_code not in RETRYABLE:
                log.error("worker %s: HTTP %s 는 재시도 대상이 아니다", worker_id, exc.status_code)
                return False
            if attempt == args.max_attempts:
                log.error("worker %s: %s 회 시도 후 포기 (HTTP %s)", worker_id, attempt, exc.status_code)
                return False

            delay = wait_seconds(exc.response.headers, attempt_index)
            source = "retry-after" if retry_after_seconds(exc.response.headers) else "백오프"
            log.warning("worker %s: HTTP %s -> %.3f초 대기 (%s)",
                        worker_id, exc.status_code, delay, source)
            time.sleep(delay)
            continue

        dump_headers(f"worker {worker_id} 시도 {attempt}", raw.http_response.status_code, raw.headers)
        log.info("worker %s: %s 번째 시도에서 성공", worker_id, attempt)
        return True

    return False


def main():
    args = parse_args()
    endpoint = base_url(args.endpoint)
    log.info("PTU 배포 %s | 동시 요청 %s | 최대 시도 %s",
             args.ptu_deployment, args.burst, args.max_attempts)

    if args.burst == 1:
        results = [run_worker(1, endpoint, args)]
    else:
        with ThreadPoolExecutor(max_workers=args.burst) as pool:
            results = list(pool.map(lambda i: run_worker(i, endpoint, args),
                                    range(1, args.burst + 1)))

    log.info("성공 %s / 전체 %s", sum(results), len(results))
    if not all(results):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
