"""Microsoft Foundry PTU 샘플 공용 모듈.

세 개의 실행 스크립트(basic / 429-retry / 429-spillover)가 공통으로 쓰는
설정 로딩, 클라이언트 생성, 추론 호출, 응답 헤더 덤프를 담는다.

설계 원칙
- 모든 설정 객체는 frozen dataclass 로 만들어 이후 단계에서 변형하지 않는다.
- 모든 호출은 ``with_raw_response`` 로 보내 원본 HTTP 헤더를 항상 볼 수 있게 한다.
- 429 를 포함한 오류 응답도 헤더를 반환해 호출자가 재시도 판단을 할 수 있게 한다.
"""

from __future__ import annotations

import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Callable, Mapping, TypeVar

import openai
from openai import OpenAI

# ---------------------------------------------------------------------------
# 상수
# ---------------------------------------------------------------------------

#: Foundry v1 엔드포인트에 붙일 때 사용하는 Entra ID 토큰 스코프.
#: 구형(classic) Azure OpenAI 데이터플레인은 "https://cognitiveservices.azure.com/.default" 를 쓴다.
DEFAULT_TOKEN_SCOPE = "https://ai.azure.com/.default"

#: v1 엔드포인트는 반드시 이 경로로 끝나야 한다. 아니면 404 가 난다.
REQUIRED_ENDPOINT_SUFFIX = "/openai/v1/"

DEFAULT_MODE = "image"
SUPPORTED_MODES = ("image", "chat")

DEFAULT_IMAGE_PROMPT = "A cute baby polar bear"
DEFAULT_CHAT_PROMPT = "Explain the purpose of an API in one sentence."
DEFAULT_IMAGE_SIZE = "1024x1024"
DEFAULT_IMAGE_COUNT = 1

#: PTU 사용률은 prompt 토큰 + max_tokens 추정치로 계산된다.
#: 실제 생성량에 가깝게 잡아야 동시 처리량이 올라간다.
DEFAULT_MAX_OUTPUT_TOKENS = 256

#: SDK 자동 재시도. 샘플에서는 재시도 동작을 눈으로 보기 위해 항상 0 으로 끈다.
SDK_AUTO_RETRIES = 0

#: 스필오버를 유발하는 상태 코드. (PTU 소진 429, 롱컨텍스트 400, 서버 오류 500/503)
SPILLOVER_STATUS_CODES = frozenset({400, 429, 500, 503})

#: 클라이언트 재시도 대상 상태 코드.
RETRYABLE_STATUS_CODES = frozenset({408, 429, 500, 502, 503, 504})


# ---------------------------------------------------------------------------
# 헤더 분류
# ---------------------------------------------------------------------------

#: 그룹 이름 -> 해당 그룹으로 묶을 헤더 이름(소문자).
HEADER_GROUPS: Mapping[str, tuple[str, ...]] = {
    "스로틀링 / 재시도": (
        "retry-after",
        "retry-after-ms",
        "x-ratelimit-limit-requests",
        "x-ratelimit-limit-tokens",
        "x-ratelimit-remaining-requests",
        "x-ratelimit-remaining-tokens",
        "x-ratelimit-reset-requests",
        "x-ratelimit-reset-tokens",
    ),
    "스필오버": (
        "x-ms-deployment-name",
        "x-ms-spillover-from-deployment",
        "x-ms-spillover-error",
    ),
    "추적 / 진단": (
        "apim-request-id",
        "x-request-id",
        "x-ms-request-id",
        "x-ms-client-request-id",
        "x-ms-region",
        "azureml-model-session",
        "openai-processing-ms",
        "openai-model",
        "x-envoy-upstream-service-time",
    ),
}

_GROUPED_HEADER_NAMES = frozenset(
    name for names in HEADER_GROUPS.values() for name in names
)

_SEPARATOR_WIDTH = 78


# ---------------------------------------------------------------------------
# 설정
# ---------------------------------------------------------------------------


class ConfigError(RuntimeError):
    """필수 환경 변수가 없거나 값이 잘못된 경우."""


@dataclass(frozen=True)
class DeploymentTarget:
    """호출 대상 배포 하나를 가리키는 불변 값."""

    endpoint: str
    deployment: str
    label: str


@dataclass(frozen=True)
class Settings:
    """스크립트 실행에 필요한 전체 설정."""

    ptu: DeploymentTarget
    standard: DeploymentTarget | None
    api_key: str | None
    token_scope: str
    mode: str
    prompt: str
    image_size: str
    max_output_tokens: int


def _normalize_endpoint(raw: str) -> str:
    """엔드포인트를 v1 데이터플레인 형식으로 정규화한다."""
    endpoint = raw.strip().rstrip("/")
    if endpoint.endswith("/openai/v1"):
        return endpoint + "/"
    return endpoint + REQUIRED_ENDPOINT_SUFFIX


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError(f"환경 변수 {name} 가 설정되지 않았습니다. README 의 '환경 변수' 절을 참고하세요.")
    return value


def _optional(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def positive_int_env(name: str, default: int) -> int:
    """환경 변수를 1 이상의 정수로 읽는다. 값이 없으면 default."""
    raw = _optional(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"환경 변수 {name} 는 정수여야 합니다 (받은 값: {raw!r}).") from exc
    if value <= 0:
        raise ConfigError(f"환경 변수 {name} 는 1 이상이어야 합니다 (받은 값: {value}).")
    return value


def load_settings(*, require_standard: bool = False) -> Settings:
    """환경 변수에서 설정을 읽어 불변 Settings 를 만든다.

    Args:
        require_standard: 표준(PayGo) 배포 설정이 필수인지 여부.

    Raises:
        ConfigError: 필수 값이 없거나 형식이 잘못된 경우.
    """
    ptu_endpoint = _normalize_endpoint(_require("FOUNDRY_ENDPOINT"))
    ptu = DeploymentTarget(
        endpoint=ptu_endpoint,
        deployment=_require("FOUNDRY_PTU_DEPLOYMENT"),
        label="PTU",
    )

    standard_deployment = _optional("FOUNDRY_STANDARD_DEPLOYMENT")
    if not standard_deployment and require_standard:
        raise ConfigError(
            "환경 변수 FOUNDRY_STANDARD_DEPLOYMENT 가 필요합니다. "
            "스필오버 대상이 될 표준(PayGo) 배포 이름을 지정하세요."
        )

    standard: DeploymentTarget | None = None
    if standard_deployment:
        standard_endpoint_raw = _optional("FOUNDRY_STANDARD_ENDPOINT")
        standard = DeploymentTarget(
            endpoint=_normalize_endpoint(standard_endpoint_raw) if standard_endpoint_raw else ptu_endpoint,
            deployment=standard_deployment,
            label="Standard(PayGo)",
        )

    mode = _optional("FOUNDRY_MODE", DEFAULT_MODE).lower()
    if mode not in SUPPORTED_MODES:
        raise ConfigError(
            f"FOUNDRY_MODE 는 {SUPPORTED_MODES} 중 하나여야 합니다 (받은 값: {mode!r})."
        )

    default_prompt = DEFAULT_IMAGE_PROMPT if mode == "image" else DEFAULT_CHAT_PROMPT

    return Settings(
        ptu=ptu,
        standard=standard,
        api_key=_optional("FOUNDRY_API_KEY") or None,
        token_scope=_optional("FOUNDRY_TOKEN_SCOPE", DEFAULT_TOKEN_SCOPE),
        mode=mode,
        prompt=_optional("FOUNDRY_PROMPT", default_prompt),
        image_size=_optional("FOUNDRY_IMAGE_SIZE", DEFAULT_IMAGE_SIZE),
        max_output_tokens=positive_int_env("FOUNDRY_MAX_TOKENS", DEFAULT_MAX_OUTPUT_TOKENS),
    )


# ---------------------------------------------------------------------------
# 클라이언트
# ---------------------------------------------------------------------------


def _build_credential_api_key(settings: Settings) -> Any:
    """API 키가 있으면 그대로, 없으면 Entra ID 토큰 프로바이더를 돌려준다."""
    if settings.api_key:
        return settings.api_key

    try:
        from azure.identity import DefaultAzureCredential, get_bearer_token_provider
    except ImportError as exc:  # pragma: no cover - 설치 누락 안내용
        raise ConfigError(
            "azure-identity 가 설치되어 있지 않습니다. `pip install azure-identity` 를 실행하거나 "
            "FOUNDRY_API_KEY 를 설정하세요."
        ) from exc

    return get_bearer_token_provider(DefaultAzureCredential(), settings.token_scope)


def build_client(settings: Settings, target: DeploymentTarget) -> OpenAI:
    """지정한 배포를 향하는 OpenAI v1 클라이언트를 만든다.

    SDK 자동 재시도는 끈다. 재시도/스필오버 동작을 샘플 코드에서 직접
    제어하고 매 시도마다 응답 헤더를 출력하기 위해서다.
    """
    return OpenAI(
        base_url=target.endpoint,
        api_key=_build_credential_api_key(settings),
        max_retries=SDK_AUTO_RETRIES,
    )


# ---------------------------------------------------------------------------
# 추론 호출
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CallResult:
    """한 번의 HTTP 호출 결과 (성공/실패 공통)."""

    target: DeploymentTarget
    status_code: int
    headers: Mapping[str, str]
    payload: Any | None
    error: Exception | None

    @property
    def is_success(self) -> bool:
        return self.error is None


def _invoke_raw(client: OpenAI, settings: Settings, target: DeploymentTarget,
                extra_headers: Mapping[str, str] | None) -> Any:
    """모드에 맞는 엔드포인트를 with_raw_response 로 호출한다."""
    headers = dict(extra_headers) if extra_headers else None

    if settings.mode == "image":
        return client.images.with_raw_response.generate(
            model=target.deployment,
            prompt=settings.prompt,
            n=DEFAULT_IMAGE_COUNT,
            size=settings.image_size,
            extra_headers=headers,
        )

    return client.chat.completions.with_raw_response.create(
        model=target.deployment,
        messages=[{"role": "user", "content": settings.prompt}],
        # PTU 사용률 추정에 직접 쓰이므로 실제 생성량에 가깝게 지정한다.
        max_completion_tokens=settings.max_output_tokens,
        extra_headers=headers,
    )


def call_deployment(
    client: OpenAI,
    settings: Settings,
    target: DeploymentTarget,
    *,
    extra_headers: Mapping[str, str] | None = None,
) -> CallResult:
    """배포를 호출하고 성공/실패 모두 헤더를 담은 CallResult 로 돌려준다.

    예외를 밖으로 던지지 않는다. 호출자가 상태 코드와 헤더를 보고
    재시도할지 스필오버할지 결정하게 하기 위해서다.
    """
    try:
        raw = _invoke_raw(client, settings, target, extra_headers)
    except openai.APIStatusError as exc:
        return CallResult(
            target=target,
            status_code=exc.status_code,
            headers=dict(exc.response.headers),
            payload=None,
            error=exc,
        )
    except openai.APIConnectionError as exc:
        return CallResult(target=target, status_code=0, headers={}, payload=None, error=exc)

    return CallResult(
        target=target,
        status_code=raw.http_response.status_code,
        headers=dict(raw.headers),
        payload=raw.parse(),
        error=None,
    )


# ---------------------------------------------------------------------------
# 동시 실행 / 출력 동기화
# ---------------------------------------------------------------------------

#: 여러 워커가 동시에 헤더를 출력할 때 줄이 섞이지 않도록 공유하는 락.
PRINT_LOCK = threading.Lock()

_T = TypeVar("_T")


def run_workers(worker_fn: Callable[[int], _T], count: int) -> list[_T]:
    """worker_fn(worker_id) 를 count 개 동시에 실행하고 결과를 모은다.

    count 가 1 이면 스레드를 만들지 않고 그대로 호출한다. PTU 429 를 실제로
    유발하려면 동시 요청이 필요하므로, 부하 생성 용도로 쓴다.
    """
    if count == 1:
        return [worker_fn(1)]

    with ThreadPoolExecutor(max_workers=count) as pool:
        futures = [pool.submit(worker_fn, worker_id) for worker_id in range(1, count + 1)]
        return [future.result() for future in futures]


# ---------------------------------------------------------------------------
# 헤더 출력 / 해석
# ---------------------------------------------------------------------------


def _print_rule(title: str = "") -> None:
    if title:
        print(f"\n{'=' * 4} {title} " + "=" * max(0, _SEPARATOR_WIDTH - len(title) - 6))
    else:
        print("=" * _SEPARATOR_WIDTH)


def print_response_headers(result: CallResult, *, title: str) -> None:
    """응답 헤더 전체를 그룹으로 나눠 출력한다.

    분류에 없는 헤더도 '기타' 로 모두 출력한다. 서비스가 새 헤더를 추가해도
    샘플이 놓치지 않게 하기 위해서다.
    """
    _print_rule(title)
    print(f"target      : {result.target.label} / {result.target.deployment}")
    print(f"endpoint    : {result.target.endpoint}")
    print(f"http status : {result.status_code}")

    if not result.headers:
        print("(응답 헤더 없음 - 연결 단계에서 실패했을 수 있습니다)")
        return

    lowered = {key.lower(): value for key, value in result.headers.items()}

    for group_name, header_names in HEADER_GROUPS.items():
        present = [(name, lowered[name]) for name in header_names if name in lowered]
        if not present:
            continue
        print(f"\n[{group_name}]")
        for name, value in present:
            print(f"  {name}: {value}")

    others = sorted(
        (name, value) for name, value in lowered.items() if name not in _GROUPED_HEADER_NAMES
    )
    if others:
        print("\n[기타]")
        for name, value in others:
            print(f"  {name}: {value}")


def retry_after_seconds(headers: Mapping[str, str]) -> float | None:
    """retry-after-ms / retry-after 헤더에서 대기 시간(초)을 뽑는다.

    PTU 배포는 429 와 함께 두 헤더를 모두 돌려준다. 밀리초 헤더가 더
    정밀하므로 우선한다. 값이 없거나 파싱 불가면 None.
    """
    lowered = {key.lower(): value for key, value in headers.items()}

    raw_ms = lowered.get("retry-after-ms")
    if raw_ms:
        try:
            return int(raw_ms) / 1000.0
        except ValueError:
            pass

    raw_seconds = lowered.get("retry-after")
    if raw_seconds:
        try:
            return float(raw_seconds)
        except ValueError:
            pass

    return None


@dataclass(frozen=True)
class SpilloverInfo:
    """응답 헤더로 판단한 서비스 측 스필오버 상태."""

    did_spill_over: bool
    served_by_deployment: str | None
    spilled_from_deployment: str | None
    origin_error_code: str | None


def inspect_spillover(headers: Mapping[str, str]) -> SpilloverInfo:
    """서비스 측(모델 배포 설정) 스필오버가 일어났는지 헤더로 판정한다.

    - x-ms-spillover-from-deployment: 존재하면 이 요청은 스필오버된 요청.
    - x-ms-deployment-name: 실제로 요청을 처리한 배포 이름.
    - x-ms-spillover-error: 스필오버를 유발한 PTU 쪽 응답 코드(429/500/503 등).
    """
    lowered = {key.lower(): value for key, value in headers.items()}
    spilled_from = lowered.get("x-ms-spillover-from-deployment")
    return SpilloverInfo(
        did_spill_over=bool(spilled_from),
        served_by_deployment=lowered.get("x-ms-deployment-name"),
        spilled_from_deployment=spilled_from,
        origin_error_code=lowered.get("x-ms-spillover-error"),
    )


def print_spillover_verdict(result: CallResult) -> None:
    """서비스 측 스필오버 판정 결과를 사람이 읽을 형태로 출력한다."""
    info = inspect_spillover(result.headers)
    print("\n[스필오버 판정]")

    if info.did_spill_over:
        print(f"  → 서비스 측 스필오버 발생: {info.spilled_from_deployment} (PTU) → "
              f"{info.served_by_deployment or '(알 수 없음)'} (Standard)")
        print(f"  → PTU 배포가 돌려준 원본 상태 코드: {info.origin_error_code or '(없음)'}")
        return

    if info.served_by_deployment:
        print(f"  → 스필오버 없음. {info.served_by_deployment} 가 직접 처리했습니다.")
    else:
        print("  → 스필오버 헤더가 없습니다. 배포에 spilloverDeploymentName 이 설정되지 않았거나,")
        print("     PTU 가 여유 있어 스필오버가 트리거되지 않았습니다.")


def describe_payload(result: CallResult, settings: Settings) -> str:
    """응답 본문을 한 줄 요약한다 (이미지 바이트는 길이만)."""
    if not result.is_success or result.payload is None:
        return "(본문 없음)"

    if settings.mode == "image":
        data = getattr(result.payload, "data", None) or []
        if not data:
            return "이미지 0장"
        b64 = getattr(data[0], "b64_json", None)
        return f"이미지 {len(data)}장 (첫 장 base64 {len(b64)}자)" if b64 else f"이미지 {len(data)}장"

    choices = getattr(result.payload, "choices", None) or []
    if not choices:
        return "(choices 없음)"
    content = (choices[0].message.content or "").strip().replace("\n", " ")
    usage = getattr(result.payload, "usage", None)
    suffix = f" | usage: {usage.prompt_tokens}+{usage.completion_tokens}" if usage else ""
    return f"{content[:160]}{suffix}"


def fail(message: str) -> None:
    """오류 메시지를 stderr 로 출력하고 종료한다."""
    print(f"[오류] {message}", file=sys.stderr)
    raise SystemExit(1)
