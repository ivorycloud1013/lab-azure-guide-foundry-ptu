"""regions.json 을 읽고 검증한다."""

import json
import logging
from dataclasses import dataclass
from types import MappingProxyType

from token_usage import PROFILES

log = logging.getLogger("multi-region")

ENDPOINT_SCHEME = "https://"
ENDPOINT_SUFFIX = "/openai/v1"


@dataclass(frozen=True)
class Region:
    name: str
    endpoint: str
    deployment: str
    ptu: int


@dataclass(frozen=True)
class RoutingConfig:
    model: str
    regions: MappingProxyType   # 리전 이름 -> Region
    routes: MappingProxyType    # 클라이언트(AWS) 리전 -> Azure 리전 우선순위


def _require_text(data, key, where):
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{where}: {key} 는 비어 있지 않은 문자열이어야 한다")
    return value


def _parse_region(name, data, default_deployment):
    where = f"regions.{name}"
    if not isinstance(data, dict):
        raise ValueError(f"{where}: 객체여야 한다")
    endpoint = _require_text(data, "endpoint", where)
    if not endpoint.startswith(ENDPOINT_SCHEME):
        raise ValueError(f"{where}: endpoint 는 https:// 로 시작해야 한다")
    if not endpoint.rstrip("/").endswith(ENDPOINT_SUFFIX):
        log.warning("%s: 엔드포인트가 /openai/v1/ 로 끝나지 않는다. 404 가 날 수 있다", where)
    ptu = data.get("ptu")
    # bool 은 int 의 하위 타입이라 따로 거른다.
    if isinstance(ptu, bool) or not isinstance(ptu, int) or ptu <= 0:
        raise ValueError(f"{where}: ptu 는 양의 정수여야 한다")
    deployment = data.get("deployment", default_deployment)
    if not isinstance(deployment, str) or not deployment:
        raise ValueError(f"{where}: deployment 가 없다")
    return Region(name=name, endpoint=endpoint, deployment=deployment, ptu=ptu)


def _parse_route(client_region, order, regions):
    where = f"routes.{client_region}"
    if not isinstance(order, list) or not order:
        raise ValueError(f"{where}: 리전 이름 목록이어야 한다")
    unknown = [name for name in order if name not in regions]
    if unknown:
        raise ValueError(f"{where}: regions 에 없는 리전 {unknown}")
    if len(set(order)) != len(order):
        raise ValueError(f"{where}: 리전이 중복됐다")
    return tuple(order)


def parse_config(data):
    if not isinstance(data, dict):
        raise ValueError("설정 최상위는 객체여야 한다")
    model = _require_text(data, "model", "설정")
    if model not in PROFILES:
        raise ValueError(f"model {model!r} 은 지원하지 않는다. 지원: {', '.join(sorted(PROFILES))}")
    default_deployment = _require_text(data, "deployment", "설정")

    raw_regions = data.get("regions")
    if not isinstance(raw_regions, dict) or not raw_regions:
        raise ValueError("regions 가 비어 있다")
    regions = {name: _parse_region(name, value, default_deployment)
               for name, value in raw_regions.items()}

    raw_routes = data.get("routes")
    if not isinstance(raw_routes, dict) or not raw_routes:
        raise ValueError("routes 가 비어 있다")
    routes = {client: _parse_route(client, order, regions)
              for client, order in raw_routes.items()}

    return RoutingConfig(model=model,
                         regions=MappingProxyType(regions),
                         routes=MappingProxyType(routes))


def load_config(path):
    try:
        with open(path, encoding="utf-8") as source:
            data = json.load(source)
    except FileNotFoundError as exc:
        raise ValueError(f"설정 파일이 없다: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"설정 파일 JSON 이 잘못됐다: {path}: {exc}") from exc
    return parse_config(data)


def region_order(config, client_region):
    order = config.routes.get(client_region)
    if order is None:
        raise ValueError(f"클라이언트 리전 {client_region!r} 의 route 가 없다. "
                         f"정의된 route: {', '.join(sorted(config.routes))}")
    return order
