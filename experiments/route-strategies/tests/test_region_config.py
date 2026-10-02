import json

import pytest

from region_config import load_config, region_order

VALID = {
    "model": "gpt-image-2",
    "deployment": "gpt-image-2",
    "regions": {
        "eastus2": {"endpoint": "https://eus2.openai.azure.com/openai/v1/", "ptu": 100},
        "australiaeast": {
            "endpoint": "https://aue.openai.azure.com/openai/v1/",
            "ptu": 100,
            "deployment": "gpt-image-2-aue",
        },
    },
    "routes": {
        "us-east-2": ["eastus2", "australiaeast"],
        "ap-southeast-2": ["australiaeast", "eastus2"],
    },
}


def write(tmp_path, data):
    path = tmp_path / "regions.json"
    path.write_text(json.dumps(data))
    return path


def with_changes(**changes):
    return {**VALID, **changes}


def test_loads_regions_with_deployment_fallback(tmp_path):
    config = load_config(write(tmp_path, VALID))

    assert config.model == "gpt-image-2"
    assert config.regions["eastus2"].deployment == "gpt-image-2"
    assert config.regions["australiaeast"].deployment == "gpt-image-2-aue"
    assert config.regions["eastus2"].ptu == 100


def test_region_order_follows_client_region(tmp_path):
    config = load_config(write(tmp_path, VALID))

    assert region_order(config, "ap-southeast-2") == ("australiaeast", "eastus2")


def test_unknown_client_region_lists_known_routes(tmp_path):
    config = load_config(write(tmp_path, VALID))

    with pytest.raises(ValueError, match="us-east-2"):
        region_order(config, "eu-west-1")


def test_route_to_undefined_region_is_rejected(tmp_path):
    data = with_changes(routes={"us-east-2": ["eastus2", "westus3"]})

    with pytest.raises(ValueError, match="westus3"):
        load_config(write(tmp_path, data))


def test_duplicate_region_in_route_is_rejected(tmp_path):
    data = with_changes(routes={"us-east-2": ["eastus2", "eastus2"]})

    with pytest.raises(ValueError, match="중복"):
        load_config(write(tmp_path, data))


@pytest.mark.parametrize("ptu", [0, -100, "100", 1.5])
def test_non_positive_integer_ptu_is_rejected(tmp_path, ptu):
    regions = {**VALID["regions"], "eastus2": {**VALID["regions"]["eastus2"], "ptu": ptu}}

    with pytest.raises(ValueError, match="ptu"):
        load_config(write(tmp_path, with_changes(regions=regions)))


def test_non_https_endpoint_is_rejected(tmp_path):
    regions = {**VALID["regions"],
               "eastus2": {"endpoint": "http://eus2.openai.azure.com/openai/v1/", "ptu": 100}}

    with pytest.raises(ValueError, match="https"):
        load_config(write(tmp_path, with_changes(regions=regions)))


def test_unsupported_model_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="model"):
        load_config(write(tmp_path, with_changes(model="gpt-2")))


def test_missing_deployment_is_rejected(tmp_path):
    data = {key: value for key, value in VALID.items() if key != "deployment"}

    with pytest.raises(ValueError, match="deployment"):
        load_config(write(tmp_path, data))


def test_missing_file_raises_value_error(tmp_path):
    with pytest.raises(ValueError, match="설정 파일"):
        load_config(tmp_path / "nope.json")


def test_invalid_json_raises_value_error(tmp_path):
    path = tmp_path / "regions.json"
    path.write_text("{not json")

    with pytest.raises(ValueError, match="JSON"):
        load_config(path)
