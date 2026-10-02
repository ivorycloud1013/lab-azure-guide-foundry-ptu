from types import SimpleNamespace

import openai
import pytest

try:  # openai 3.x 는 httpx2, 1.x·2.x 는 httpx 를 쓴다.
    import httpx2 as httpx
except ImportError:
    import httpx

from foundry_client import (
    DEFAULT_TOKEN_SCOPE,
    api_key_env_name,
    make_sender,
    resolve_auth,
    resolve_api_keys,
)
from region_config import Region
from token_usage import API_CHAT_COMPLETIONS, API_IMAGES_GENERATE

REGIONS = {
    "eastus2": Region("eastus2", "https://eus2.openai.azure.com/openai/v1/", "dep-eus2", 100),
}
REQUEST = httpx.Request("POST", "https://eus2.openai.azure.com/openai/v1/images/generations")


class FakeRaw:
    def __init__(self, usage, headers):
        self._usage = usage
        self.headers = headers

    def parse(self):
        usage = None if self._usage is None else SimpleNamespace(model_dump=lambda: self._usage)
        return SimpleNamespace(usage=usage)


class FakeClient:
    """openai 클라이언트 중 with_raw_response 경로만 흉내 낸다."""

    def __init__(self, behaviour):
        self.behaviour = behaviour
        self.calls = []
        create = SimpleNamespace(create=self._invoke)
        generate = SimpleNamespace(generate=self._invoke)
        self.chat = SimpleNamespace(completions=SimpleNamespace(with_raw_response=create))
        self.images = SimpleNamespace(with_raw_response=generate)

    def _invoke(self, **kwargs):
        self.calls.append(kwargs)
        return self.behaviour()


def status_error(status, headers):
    response = httpx.Response(status, headers=headers, request=REQUEST)
    return openai.APIStatusError("boom", response=response, body=None)


def test_resolve_auth_defaults_to_entra_id():
    assert resolve_auth("entra-id") == (False, DEFAULT_TOKEN_SCOPE)


def test_resolve_auth_accepts_custom_scope():
    assert resolve_auth("entra-id=api://x/.default") == (False, "api://x/.default")


def test_resolve_auth_api_key_mode():
    assert resolve_auth("api-key") == (True, None)


@pytest.mark.parametrize("value", ["entra-id=", "api-key=abc", "basic"])
def test_resolve_auth_rejects_other_values(value):
    with pytest.raises(ValueError):
        resolve_auth(value)


def test_api_key_env_name_is_upper_snake():
    assert api_key_env_name("australiaeast") == "FOUNDRY_API_KEY_AUSTRALIAEAST"
    assert api_key_env_name("west-us.3") == "FOUNDRY_API_KEY_WEST_US_3"


def test_resolve_api_keys_reads_each_region(monkeypatch):
    monkeypatch.setenv("FOUNDRY_API_KEY_EASTUS2", "k1")

    assert resolve_api_keys(["eastus2"]) == {"eastus2": "k1"}


def test_resolve_api_keys_names_all_missing_variables(monkeypatch):
    monkeypatch.delenv("FOUNDRY_API_KEY_EASTUS2", raising=False)
    monkeypatch.delenv("FOUNDRY_API_KEY_AUSTRALIAEAST", raising=False)

    with pytest.raises(ValueError, match="FOUNDRY_API_KEY_EASTUS2.*FOUNDRY_API_KEY_AUSTRALIAEAST"):
        resolve_api_keys(["eastus2", "australiaeast"])


def test_sender_returns_usage_and_headers_on_success():
    usage = {"input_tokens": 1, "output_tokens": 2}
    client = FakeClient(lambda: FakeRaw(usage, {"x-ms-region": "East US 2"}))
    send = make_sender({"eastus2": client}, REGIONS, API_IMAGES_GENERATE, "cat", 256)

    result = send("eastus2")

    assert (result.status, result.usage) == (200, usage)
    assert result.headers["x-ms-region"] == "East US 2"
    assert client.calls[0]["model"] == "dep-eus2"


def test_sender_passes_max_tokens_for_chat():
    client = FakeClient(lambda: FakeRaw(None, {}))
    send = make_sender({"eastus2": client}, REGIONS, API_CHAT_COMPLETIONS, "hi", 64)

    result = send("eastus2")

    assert result.usage is None
    assert client.calls[0]["max_completion_tokens"] == 64


def test_sender_maps_status_error_with_headers():
    def raise_429():
        raise status_error(429, {"retry-after-ms": "500"})
    send = make_sender({"eastus2": FakeClient(raise_429)}, REGIONS, API_IMAGES_GENERATE, "cat", 0)

    result = send("eastus2")

    assert (result.status, result.headers["retry-after-ms"]) == (429, "500")
    assert result.error == "APIStatusError"


def test_sender_maps_connection_error_to_no_status():
    def raise_timeout():
        raise openai.APITimeoutError(request=REQUEST)
    send = make_sender({"eastus2": FakeClient(raise_timeout)}, REGIONS, API_IMAGES_GENERATE, "cat", 0)

    result = send("eastus2")

    assert (result.status, result.error) == (None, "APITimeoutError")


def test_sender_maps_unparseable_body_to_failure():
    class BrokenRaw(FakeRaw):
        def parse(self):
            response = httpx.Response(200, headers={"x-ms-region": "East US 2"}, request=REQUEST)
            raise openai.APIResponseValidationError(response=response, body=None)
    send = make_sender({"eastus2": FakeClient(lambda: BrokenRaw(None, {}))},
                       REGIONS, API_IMAGES_GENERATE, "cat", 0)

    result = send("eastus2")

    assert (result.status, result.error) == (None, "APIResponseValidationError")


class FakeCredential:
    def __init__(self, lifetime):
        self.lifetime = lifetime
        self.calls = []

    def get_token(self, scope, tenant_id=None):
        self.calls.append((scope, tenant_id))
        return SimpleNamespace(token=f"t{len(self.calls)}", expires_on=CLOCK.now + self.lifetime)


from conftest import FakeClock  # noqa: E402
from foundry_client import TOKEN_REFRESH_MARGIN_SECONDS, tenant_token_provider  # noqa: E402

CLOCK = FakeClock()


def test_tenant_token_provider_requests_the_given_tenant_and_caches():
    credential = FakeCredential(lifetime=3600)
    provider = tenant_token_provider(credential, "scope/.default", "tenant-b", clock=CLOCK)

    first, second = provider(), provider()

    assert (first, second) == ("t1", "t1")
    assert credential.calls == [("scope/.default", "tenant-b")]


def test_tenant_token_provider_refreshes_before_expiry():
    credential = FakeCredential(lifetime=3600)
    provider = tenant_token_provider(credential, "scope/.default", "tenant-b", clock=CLOCK)
    provider()

    CLOCK.advance(3600 - TOKEN_REFRESH_MARGIN_SECONDS + 1)

    assert provider() == "t2"


def test_sender_keeps_the_error_body_for_non_429_failures():
    def raise_404():
        raise status_error(404, {"apim-request-id": "abc"})
    send = make_sender({"eastus2": FakeClient(raise_404)}, REGIONS, API_IMAGES_GENERATE, "cat", 0)

    result = send("eastus2")

    assert result.status == 404
    assert result.detail  # 본문이 있으면 그대로, 없으면 빈 문자열이 아니어야 한다
