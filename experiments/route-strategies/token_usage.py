"""응답 body 의 usage 를 PTU 가 소비하는 정규화 토큰으로 바꾼다.

PTU 응답에는 남은 용량을 알려주는 헤더가 없다. 대신 usage 를 sizing 문서의
공식으로 환산하면 요청 하나가 PTU 를 얼마나 썼는지 알 수 있다.
https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/provisioned-throughput-sizing
"""

import math
import threading
from dataclasses import dataclass

API_IMAGES_GENERATE = "images.generate"
API_CHAT_COMPLETIONS = "chat.completions"

# 토크나이저 없이 프롬프트 토큰을 어림잡는 비율. 예약용 추정치라 대략이면 된다.
CHARS_PER_TOKEN = 4
# 이미지 출력 토큰의 첫 추정치. gpt-image-2 1024x1024 기본 품질의 실측값(2026-09)이며,
# 첫 응답을 받은 뒤로는 실제 usage 로 바뀐다. sizing 문서 예시(7,024)로 잡으면 실측의
# 35배라 첫 응답 전까지 리전당 2~3건만 보낸다.
DEFAULT_IMAGE_OUTPUT_TOKENS = 196


@dataclass(frozen=True)
class ModelProfile:
    """sizing 문서의 모델별 처리량 값.

    tpm_per_ptu         PTU 1개가 분당 처리하는 정규화 토큰(= 텍스트 입력 토큰)
    output_ratio        텍스트 출력 토큰 1개가 입력 토큰 몇 개로 계산되는지
    image_input_factor  이미지 토큰 1개가 텍스트 입력 토큰 몇 개로 계산되는지
    image_output_ratio  이미지 출력 토큰이 이미지 입력 토큰보다 몇 배 무거운지
    """

    tpm_per_ptu: int
    output_ratio: float = 0.0
    image_input_factor: float = 0.0
    image_output_ratio: float = 0.0


PROFILES = {
    "gpt-image-2": ModelProfile(1200, image_input_factor=1.6, image_output_ratio=3.75),
    "gpt-5.5": ModelProfile(1200, output_ratio=6),
    "gpt-5.4": ModelProfile(2400, output_ratio=6),
    "gpt-5.4-mini": ModelProfile(7900, output_ratio=6),
    "gpt-5.2": ModelProfile(3400, output_ratio=8),
    "gpt-5.1": ModelProfile(4750, output_ratio=8),
    "gpt-5": ModelProfile(4750, output_ratio=8),
    "gpt-5-mini": ModelProfile(23750, output_ratio=8),
    "gpt-4.1": ModelProfile(3000, output_ratio=4),
    "gpt-4.1-mini": ModelProfile(14900, output_ratio=4),
    "gpt-4.1-nano": ModelProfile(59400, output_ratio=4),
}


def get_profile(model):
    profile = PROFILES.get(model)
    if profile is None:
        raise ValueError(f"지원하지 않는 model: {model!r}. 지원: {', '.join(sorted(PROFILES))}")
    return profile


def _detail(usage, group, name):
    return (usage.get(group) or {}).get(name) or 0


def _chat_cost(profile, usage):
    cached = _detail(usage, "prompt_tokens_details", "cached_tokens")
    uncached_input = usage["prompt_tokens"] - cached
    return uncached_input + profile.output_ratio * (usage.get("completion_tokens") or 0)


def _image_cost(profile, usage):
    image_input = _detail(usage, "input_tokens_details", "image_tokens")
    text_input = usage["input_tokens"] - image_input
    image_output = usage.get("output_tokens") or 0
    factor = profile.image_input_factor
    return text_input + image_input * factor + image_output * profile.image_output_ratio * factor


def normalized_cost(profile, usage):
    """usage dict 를 정규화 토큰 수로 바꾼다. chat 과 images 의 usage 모양이 다르다."""
    if "prompt_tokens" in usage:
        return _chat_cost(profile, usage)
    if "input_tokens" in usage:
        return _image_cost(profile, usage)
    raise ValueError(f"usage 형식을 알 수 없다: {sorted(usage)}")


def _prompt_tokens(prompt):
    return math.ceil(len(prompt) / CHARS_PER_TOKEN)


class CostEstimator:
    """요청을 보내기 전에 예약할 정규화 토큰을 추정한다.

    chat 은 서비스와 같은 방식으로 prompt + max_tokens 를 예약한다.
    이미지는 max_tokens 가 없으므로 직전 응답의 실제 비용을 다음 추정치로 쓴다.
    """

    def __init__(self, profile, api, prompt, max_tokens):
        self._profile = profile
        self._api = api
        self._prompt_tokens = _prompt_tokens(prompt)
        self._max_tokens = max_tokens
        self._lock = threading.Lock()
        self._last_image_cost = (
            self._prompt_tokens
            + DEFAULT_IMAGE_OUTPUT_TOKENS * profile.image_output_ratio * profile.image_input_factor
        )

    def estimate(self):
        if self._api == API_CHAT_COMPLETIONS:
            return self._prompt_tokens + self._profile.output_ratio * self._max_tokens
        with self._lock:
            return self._last_image_cost

    def actual_cost(self, usage):
        return normalized_cost(self._profile, usage)

    def observe(self, cost):
        if self._api == API_CHAT_COMPLETIONS:
            return
        with self._lock:
            self._last_image_cost = cost
