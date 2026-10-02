"""리전별 Foundry 클라이언트를 만들고 호출 결과를 CallResult 로 바꾼다."""

import os
import re
import threading
import time

import openai
from openai import OpenAI

from routing import STATUS_OK, CallResult
from token_usage import API_CHAT_COMPLETIONS

DEFAULT_TOKEN_SCOPE = "https://ai.azure.com/.default"
AUTH_ENTRA_ID = "entra-id"
AUTH_ENTRA_ID_PREFIX = AUTH_ENTRA_ID + "="
AUTH_API_KEY = "api-key"
API_KEY_ENV_PREFIX = "FOUNDRY_API_KEY_"
IMAGE_SIZE = "1024x1024"
# 429 가 아닌 실패는 원인을 알아야 하므로 본문 앞부분을 남긴다.
DETAIL_LIMIT = 300
# 토큰 만료 이 시간 전에 새로 받는다.
TOKEN_REFRESH_MARGIN_SECONDS = 300


def resolve_auth(value):
    """--auth 값을 (api_key 사용 여부, 토큰 스코프) 로 푼다.

    entra-id            기본 스코프로 Entra ID 인증 (기본값)
    entra-id=<스코프>    스코프를 지정해 Entra ID 인증
    api-key             리전별 환경변수 FOUNDRY_API_KEY_<REGION> 의 키로 인증
    """
    if value == AUTH_ENTRA_ID:
        return False, DEFAULT_TOKEN_SCOPE
    if value.startswith(AUTH_ENTRA_ID_PREFIX) and len(value) > len(AUTH_ENTRA_ID_PREFIX):
        return False, value[len(AUTH_ENTRA_ID_PREFIX):]
    if value == AUTH_API_KEY:
        return True, None
    raise ValueError(f"--auth 는 {AUTH_ENTRA_ID}, {AUTH_ENTRA_ID_PREFIX}<스코프>, "
                     f"{AUTH_API_KEY} 중 하나여야 한다. 키는 리소스마다 달라 "
                     f"환경변수 {API_KEY_ENV_PREFIX}<REGION> 으로 받는다")


def api_key_env_name(region):
    return API_KEY_ENV_PREFIX + re.sub(r"[^A-Z0-9]", "_", region.upper())


def resolve_api_keys(regions):
    """키를 명령줄에 두면 셸 기록에 남으므로 환경변수에서만 읽는다."""
    names = {region: api_key_env_name(region) for region in regions}
    missing = [name for region, name in names.items() if not os.environ.get(name)]
    if missing:
        raise ValueError(f"API key 환경변수가 없다: {', '.join(missing)}")
    return {region: os.environ[name] for region, name in names.items()}


def tenant_token_provider(credential, scope, tenant_id, clock=time.time):
    """지정한 테넌트의 토큰을 받아 만료 직전까지 재사용하는 provider.

    get_bearer_token_provider 는 테넌트를 넘길 수 없다. az 기본 로그인 테넌트와
    리소스 테넌트가 다르면 "Token tenant does not match resource tenant" 400 이 난다.
    """
    lock = threading.Lock()
    cached = {"token": None, "expires_on": 0.0}

    def provide():
        with lock:
            if clock() >= cached["expires_on"] - TOKEN_REFRESH_MARGIN_SECONDS:
                access = credential.get_token(scope, tenant_id=tenant_id)
                cached.update(token=access.token, expires_on=access.expires_on)
            return cached["token"]

    return provide


def build_clients(regions, use_api_key, token_scope, tenant_id=None):
    """리전마다 클라이언트를 하나씩 만들고 스레드끼리 공유한다.

    Entra ID 토큰은 스코프가 같아 두 리소스에 모두 통하므로 자격 증명은 하나만 만든다.
    SDK 자동 재시도를 꺼서 리전 전환 시점을 라우터가 직접 정한다.
    """
    if use_api_key:
        keys = resolve_api_keys(regions)
        return {name: OpenAI(base_url=region.endpoint, api_key=keys[name], max_retries=0)
                for name, region in regions.items()}

    from azure.identity import DefaultAzureCredential, get_bearer_token_provider
    credential = DefaultAzureCredential()
    if tenant_id:
        token_provider = tenant_token_provider(credential, token_scope, tenant_id)
    else:
        token_provider = get_bearer_token_provider(credential, token_scope)
    return {name: OpenAI(base_url=region.endpoint, api_key=token_provider, max_retries=0)
            for name, region in regions.items()}


def _invoke(client, deployment, api, prompt, max_tokens):
    if api == API_CHAT_COMPLETIONS:
        return client.chat.completions.with_raw_response.create(
            model=deployment,
            messages=[{"role": "user", "content": prompt}],
            max_completion_tokens=max_tokens,
        )
    return client.images.with_raw_response.generate(
        model=deployment, prompt=prompt, n=1, size=IMAGE_SIZE,
    )


def _detail(exc):
    """429 는 헤더만 보면 되지만, 그 밖의 실패는 본문에 원인이 들어 있다."""
    if exc.status_code == 429:
        return None
    try:
        body = exc.response.text
    except Exception as read_error:  # 본문을 못 읽어도 호출은 계속돼야 한다
        return f"<본문을 읽지 못했다: {type(read_error).__name__}>"
    return (body or "<빈 본문>")[:DETAIL_LIMIT]


def make_sender(clients, regions, api, prompt, max_tokens):
    """route_request 가 쓰는 send(region) -> CallResult 를 만든다."""

    def send(region):
        try:
            raw = _invoke(clients[region], regions[region].deployment, api, prompt, max_tokens)
        except openai.APIStatusError as exc:
            return CallResult(exc.status_code, dict(exc.response.headers), None,
                              type(exc).__name__, _detail(exc))
        except openai.APIConnectionError as exc:
            return CallResult(None, {}, None, type(exc).__name__)

        try:
            usage = raw.parse().usage
        except openai.APIError as exc:
            # 200 이지만 body 를 읽지 못했다. 결과를 쓸 수 없으니 응답이 없는 것과 같이 다룬다.
            return CallResult(None, dict(raw.headers), None, type(exc).__name__)
        return CallResult(STATUS_OK, dict(raw.headers),
                          None if usage is None else usage.model_dump())

    return send
