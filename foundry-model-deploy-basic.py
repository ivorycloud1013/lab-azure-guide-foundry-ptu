#!/usr/bin/env python3

import argparse
import base64
import logging
import os

import openai
from openai import OpenAI

logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
log = logging.getLogger("basic")

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
API_IMAGES_EDIT = "images.edit"
API_CHAT_COMPLETIONS = "chat.completions"
IMAGE_APIS = (API_IMAGES_GENERATE, API_IMAGES_EDIT)

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
        description="Foundry 모델 배포를 한 번 호출하고 응답 헤더를 출력한다.")
    parser.add_argument("--endpoint", required=True,
                        help="모델 배포 엔드포인트. /openai/v1/ 까지 포함한 전체 URL")
    parser.add_argument("--deployment", required=True, help="모델 배포 이름")
    parser.add_argument("--api",
                        choices=(API_IMAGES_GENERATE, API_IMAGES_EDIT, API_CHAT_COMPLETIONS),
                        default=API_IMAGES_GENERATE,
                        help=f"호출할 API (기본 {API_IMAGES_GENERATE})")
    parser.add_argument("--auth", default=AUTH_ENTRA_ID, metavar="METHOD",
                        help=f"{AUTH_ENTRA_ID} (기본) | {AUTH_ENTRA_ID}=<스코프> | api-key=<키>")
    parser.add_argument("--prompt", help="프롬프트 (기본값은 --api 별로 다름)")
    parser.add_argument("--image", help="images.edit 의 입력 이미지 경로. images.edit 일 때 필수")
    parser.add_argument("--output-image",
                        help="images.* 결과를 저장할 경로 (기본 ./output-<api>.png)")

    args = parser.parse_args()
    args.api_key, args.token_scope = resolve_auth(args.auth, parser)
    # images.edit 는 편집 대상 이미지가 있어야 한다.
    if args.api == API_IMAGES_EDIT:
        if not args.image:
            parser.error("--api images.edit 에는 --image 로 입력 이미지를 지정해야 한다")
        if not os.path.isfile(args.image):
            parser.error(f"--image 경로에 파일이 없다: {args.image}")
    if not args.prompt:
        args.prompt = DEFAULT_IMAGE_PROMPT if args.api in IMAGE_APIS else DEFAULT_CHAT_PROMPT
    if args.api in IMAGE_APIS and not args.output_image:
        args.output_image = f"./output-{args.api.replace('.', '-')}.png"
    return args


def check_endpoint(url):
    """v1 데이터플레인 경로가 아니면 404 가 난다. 고쳐주지는 않고 경고만 한다."""
    if not url.rstrip("/").endswith("/openai/v1"):
        log.warning("엔드포인트가 /openai/v1/ 로 끝나지 않는다. 404 가 날 수 있다: %s", url)
    return url


def build_client(endpoint, api_key, token_scope):
    """키가 있으면 키 인증, 없으면 Entra ID 토큰 프로바이더를 쓴다.

    SDK 자동 재시도는 끈다. 응답 헤더를 그대로 보기 위해서다.
    """
    if api_key:
        credential = api_key
    else:
        from azure.identity import DefaultAzureCredential, get_bearer_token_provider
        credential = get_bearer_token_provider(DefaultAzureCredential(), token_scope)
    return OpenAI(base_url=endpoint, api_key=credential, max_retries=0)


def call(client, args):
    """with_raw_response 로 호출해 성공·실패 모두 원본 헤더를 얻는다."""
    if args.api == API_IMAGES_GENERATE:
        return client.images.with_raw_response.generate(
            model=args.deployment, prompt=args.prompt, n=1, size=IMAGE_SIZE,
        )
    if args.api == API_IMAGES_EDIT:
        with open(args.image, "rb") as source:
            return client.images.with_raw_response.edit(
                model=args.deployment, image=source, prompt=args.prompt,
                n=1, size=IMAGE_SIZE,
            )
    return client.chat.completions.with_raw_response.create(
        model=args.deployment,
        messages=[{"role": "user", "content": args.prompt}],
        max_completion_tokens=MAX_OUTPUT_TOKENS,
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


def report_result(payload, args):
    """응답 본문을 요약하고, images.* 는 --output-image 경로에 저장한다."""
    if args.api not in IMAGE_APIS:
        message = payload.choices[0].message.content or ""
        log.info("Answer: %s", message.strip().replace("\n", " ")[:160])
        return

    image_b64 = payload.data[0].b64_json
    if not image_b64:
        log.error("응답에 base64 이미지가 없다. 모델이 URL 로 돌려줬을 수 있다: %s", payload.data[0])
        raise SystemExit(1)

    log.info("이미지 %s장 수신 (base64 %s자)", len(payload.data), len(image_b64))
    if args.output_image:
        with open(args.output_image, "wb") as target:
            target.write(base64.b64decode(image_b64))
        log.info("%s 로 저장했다", args.output_image)


def main():
    args = parse_args()
    client = build_client(check_endpoint(args.endpoint), args.api_key, args.token_scope)

    try:
        print("\n=== Request ===")
        raw = call(client, args)
    except openai.APIStatusError as exc:
        dump_headers("Response", exc.status_code, exc.response.headers)
        log.error("호출 실패: HTTP %s", exc.status_code)
        raise SystemExit(1)

    dump_headers("Response", raw.http_response.status_code, raw.headers)
    report_result(raw.parse(), args)


if __name__ == "__main__":
    main()
