import pytest

from token_usage import (
    DEFAULT_IMAGE_OUTPUT_TOKENS,
    CostEstimator,
    get_profile,
    normalized_cost,
)


def test_chat_cost_weights_output_and_discounts_cached_input():
    # Arrange: gpt-5.2 는 출력 1 토큰 = 입력 8 토큰
    profile = get_profile("gpt-5.2")
    usage = {
        "prompt_tokens": 200,
        "completion_tokens": 20,
        "prompt_tokens_details": {"cached_tokens": 100},
    }

    # Act
    cost = normalized_cost(profile, usage)

    # Assert: (200 - 100) + 8 * 20
    assert cost == 260


def test_chat_cost_without_details_counts_all_prompt_tokens():
    profile = get_profile("gpt-4.1")

    cost = normalized_cost(profile, {"prompt_tokens": 50, "completion_tokens": 10})

    assert cost == 50 + 4 * 10


def test_image_cost_matches_sizing_doc_worked_example():
    # Arrange: sizing 문서 예시 — 텍스트 2,000 / 이미지 입력 1,229 / 이미지 출력 7,024
    profile = get_profile("gpt-image-2")
    usage = {
        "input_tokens": 3229,
        "output_tokens": 7024,
        "input_tokens_details": {"text_tokens": 2000, "image_tokens": 1229},
    }

    # Act
    cost = normalized_cost(profile, usage)

    # Assert
    assert cost == pytest.approx(46110.4)


def test_image_cost_treats_missing_details_as_text_input():
    profile = get_profile("gpt-image-2")

    cost = normalized_cost(profile, {"input_tokens": 10, "output_tokens": 100})

    assert cost == pytest.approx(10 + 100 * 3.75 * 1.6)


def test_unknown_usage_shape_raises():
    with pytest.raises(ValueError):
        normalized_cost(get_profile("gpt-5"), {"total_tokens": 10})


def test_unknown_model_raises_with_supported_list():
    with pytest.raises(ValueError, match="gpt-image-2"):
        get_profile("not-a-model")


def test_chat_estimate_reserves_prompt_and_max_tokens_like_the_service():
    # 서비스는 prompt 토큰 + max_tokens 로 먼저 예약한다.
    estimator = CostEstimator(get_profile("gpt-5"), "chat.completions",
                              prompt="x" * 40, max_tokens=100)

    assert estimator.estimate() == 10 + 8 * 100


def test_chat_estimate_ignores_observed_cost():
    estimator = CostEstimator(get_profile("gpt-5"), "chat.completions",
                              prompt="x" * 40, max_tokens=100)

    estimator.observe(5)

    assert estimator.estimate() == 810


def test_image_estimate_starts_from_default_then_follows_last_response():
    profile = get_profile("gpt-image-2")
    estimator = CostEstimator(profile, "images.generate", prompt="x" * 40, max_tokens=0)
    default = 10 + DEFAULT_IMAGE_OUTPUT_TOKENS * 3.75 * 1.6

    first = estimator.estimate()
    estimator.observe(12345.0)
    second = estimator.estimate()

    assert first == pytest.approx(default)
    assert second == 12345.0
