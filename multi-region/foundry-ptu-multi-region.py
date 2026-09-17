#!/usr/bin/env python3
"""여러 리전의 PTU 배포로 트래픽을 나눠 보낸다.

--strategy 429-spillover   429 를 받으면 다음 리전으로 넘긴다
--strategy capacity-aware  추정 사용량이 넘칠 것 같으면 보내기 전에 다음 리전을 고른다
"""

import argparse
import logging
import os
import random
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from capacity_tracker import CapacityTracker
from foundry_client import AUTH_ENTRA_ID, build_clients, make_sender, resolve_auth
from load_pattern import paced_batches, requests_per_minute, run_paced
from region_config import load_config, region_order
from routing import STRATEGIES, Primary, pickers_for, route_request
from token_usage import API_CHAT_COMPLETIONS, API_IMAGES_GENERATE, CostEstimator, get_profile

logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(threadName)s %(message)s")
log = logging.getLogger("multi-region")

DEFAULT_CONFIG = Path(__file__).resolve().parent / "regions.json"
CLIENT_REGION_ENVS = ("AWS_REGION", "AWS_DEFAULT_REGION")
DEFAULT_IMAGE_PROMPT = "A cute baby polar bear"
DEFAULT_CHAT_PROMPT = "Explain the purpose of an API in one sentence."
# PTU 사용률은 prompt 토큰 + max_tokens 로 먼저 예약된다. 실제 생성량에 가깝게 잡는다.
MAX_OUTPUT_TOKENS = 256
# 동시 요청이 같은 시각에 다시 몰리지 않도록 대기 시간에 더하는 비율.
JITTER_RATIO = 0.25
DEFAULT_INTERVAL_SECONDS = 5.0
DEFAULT_DURATION_SECONDS = 60.0
# 묶음 발송 모드의 스레드 상한. 이미지 한 장이 수십 초 걸려 동시에 떠 있는 요청이 많다.
DEFAULT_MAX_WORKERS = 256


def default_client_region():
    for name in CLIENT_REGION_ENVS:
        if os.environ.get(name):
            return os.environ[name]
    return None


def positive_int(text):
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError("1 이상이어야 한다")
    return value


def positive_float(text):
    value = float(text)
    if value <= 0:
        raise argparse.ArgumentTypeError("0 보다 커야 한다")
    return value


def headroom_ratio(text):
    value = float(text)
    if not 0 < value <= 1:
        raise argparse.ArgumentTypeError("0 초과 1 이하여야 한다")
    return value


def parse_args():
    parser = argparse.ArgumentParser(description="여러 리전의 PTU 배포로 트래픽을 나눠 보낸다.")
    parser.add_argument("--strategy", required=True, choices=STRATEGIES,
                        help="429-spillover: primary 가 429 를 주면 다음 리전을 primary 로 | "
                             "capacity-aware: 추정 사용량이 넘치기 전에 primary 를 바꿈")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG),
                        help=f"리전 설정 JSON (기본 {DEFAULT_CONFIG.name})")
    parser.add_argument("--client-region", default=default_client_region(),
                        help="이 클라이언트의 리전. routes 의 키 (기본 $AWS_REGION)")
    parser.add_argument("--auth", default=AUTH_ENTRA_ID, metavar="METHOD",
                        help="entra-id (기본) | entra-id=<스코프> | "
                             "api-key (환경변수 FOUNDRY_API_KEY_<REGION>)")
    parser.add_argument("--tenant-id", default=os.environ.get("AZURE_TENANT_ID"),
                        help="Entra ID 토큰을 받을 테넌트. 로그인 기본 테넌트와 리소스 테넌트가 "
                             "다를 때 지정한다 (기본 $AZURE_TENANT_ID)")
    parser.add_argument("--api", choices=(API_IMAGES_GENERATE, API_CHAT_COMPLETIONS),
                        default=API_IMAGES_GENERATE, help=f"호출할 API (기본 {API_IMAGES_GENERATE})")
    parser.add_argument("--prompt", help="프롬프트 (기본값은 --api 별로 다름)")
    parser.add_argument("--max-tokens", type=positive_int, default=MAX_OUTPUT_TOKENS,
                        help=f"chat.completions 전용 (기본 {MAX_OUTPUT_TOKENS})")
    parser.add_argument("--requests", type=positive_int,
                        help="동시성 모드: 보낼 요청 수 (기본 1)")
    parser.add_argument("--concurrency", type=positive_int,
                        help="동시성 모드: 동시 요청 수 (기본 1)")
    parser.add_argument("--load-ptu", type=positive_float,
                        help="묶음 발송 모드: 이 PTU 가 1분에 처리할 만큼을 --interval 마다 나눠 보낸다")
    parser.add_argument("--interval", type=positive_float, default=DEFAULT_INTERVAL_SECONDS,
                        help=f"묶음 발송 모드: 묶음 간격(초) (기본 {DEFAULT_INTERVAL_SECONDS:g})")
    parser.add_argument("--duration", type=positive_float, default=DEFAULT_DURATION_SECONDS,
                        help=f"묶음 발송 모드: 보내는 기간(초) (기본 {DEFAULT_DURATION_SECONDS:g})")
    parser.add_argument("--max-workers", type=positive_int, default=DEFAULT_MAX_WORKERS,
                        help=f"묶음 발송 모드: 동시에 떠 있을 수 있는 요청 상한 (기본 {DEFAULT_MAX_WORKERS})")
    parser.add_argument("--max-attempts", type=positive_int, default=5,
                        help="요청 하나당 최대 시도 횟수. 리전 전환도 한 번으로 센다 (기본 5)")
    parser.add_argument("--headroom", type=headroom_ratio, default=1.0,
                        help="capacity-aware 전용. 추정 용량의 이 비율까지만 채운다 (기본 1.0)")

    args = parser.parse_args()
    if args.load_ptu and (args.requests or args.concurrency):
        parser.error("--load-ptu 는 --requests·--concurrency 와 함께 쓸 수 없다")
    if args.load_ptu and args.interval > args.duration:
        parser.error("--interval 이 --duration 보다 길다")
    args.requests = args.requests or 1
    args.concurrency = args.concurrency or 1
    if not args.client_region:
        parser.error("--client-region 을 주거나 AWS_REGION 환경변수를 설정해야 한다")
    try:
        args.use_api_key, args.token_scope = resolve_auth(args.auth)
    except ValueError as exc:
        parser.error(str(exc))
    if not args.prompt:
        args.prompt = DEFAULT_IMAGE_PROMPT if args.api == API_IMAGES_GENERATE else DEFAULT_CHAT_PROMPT
    return args, parser


def jittered_sleep(seconds):
    time.sleep(seconds + seconds * JITTER_RATIO * random.random())


def log_summary(outcomes, order, tracker):
    served = Counter(outcome.region for outcome in outcomes if outcome.succeeded)
    failed = Counter(outcome.status for outcome in outcomes if not outcome.succeeded)
    attempts = sum(outcome.attempts for outcome in outcomes)

    log.info("\n=== Summary ===")
    log.info("성공 %s / 전체 %s | 총 시도 %s", sum(served.values()), len(outcomes), attempts)
    for region in order:
        log.info("  %s: 처리 %s 건 | 추정 사용률 %.0f%%",
                 region, served[region], tracker.utilization(region) * 100)
    for status, count in failed.items():
        log.info("  실패 HTTP %s: %s 건", status, count)


def run_load(args, profile, estimator, run_one):
    """--load-ptu 만큼의 요청을 --interval 마다 나눠 --duration 동안 보낸다.

    요청당 비용은 첫 추정치로 계산한다. 이미지는 실측값, chat 은 max_tokens 기준이다.
    """
    rate = requests_per_minute(args.load_ptu, profile.tpm_per_ptu, estimator.estimate())
    batches = paced_batches(rate, args.interval, args.duration)
    log.info("묶음 발송 모드 | %s PTU = 분당 %.1f 건 | %gs 마다 %s 건 | %gs 동안 총 %s 건",
             f"{args.load_ptu:g}", rate, args.interval,
             "/".join(str(size) for size in sorted({b.size for b in batches})),
             args.duration, sum(b.size for b in batches))
    return run_paced(batches, run_one, max_workers=args.max_workers)


def main():
    args, parser = parse_args()
    try:
        config = load_config(args.config)
        order = region_order(config, args.client_region)
        regions = {name: config.regions[name] for name in order}
        clients = build_clients(regions, args.use_api_key, args.token_scope, args.tenant_id)
    except ValueError as exc:
        parser.error(str(exc))

    profile = get_profile(config.model)
    capacities = {name: region.ptu * profile.tpm_per_ptu for name, region in regions.items()}
    tracker = CapacityTracker(capacities, headroom=args.headroom)
    estimator = CostEstimator(profile, args.api, args.prompt, args.max_tokens)
    send = make_sender(clients, regions, args.api, args.prompt, args.max_tokens)
    pick = pickers_for(Primary(order))[args.strategy]

    log.info("strategy %s | client %s | 순서 %s | 모델 %s",
             args.strategy, args.client_region, " -> ".join(order), config.model)
    for name, region in regions.items():
        log.info("  %s: %s PTU = 분당 %s 정규화 토큰 | 배포 %s",
                 name, region.ptu, f"{capacities[name]:,}", region.deployment)

    def run_one(index):
        return route_request(send, order, tracker, estimator, pick,
                             max_attempts=args.max_attempts, sleep=jittered_sleep,
                             label=f"req {index}")

    if args.load_ptu:
        outcomes = run_load(args, profile, estimator, run_one)
    else:
        log.info("동시성 모드 | 요청 %s | 동시 %s", args.requests, args.concurrency)
        with ThreadPoolExecutor(max_workers=args.concurrency, thread_name_prefix="w") as pool:
            outcomes = list(pool.map(run_one, range(1, args.requests + 1)))

    log_summary(outcomes, order, tracker)
    if not all(outcome.succeeded for outcome in outcomes):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
