"""리전 선택 정책과 요청 하나의 라우팅 루프.

두 정책 모두 primary 리전 하나로만 보내다가, 그 리전이 막히면 다음 리전을 새 primary
로 삼고 그대로 유지한다. 판단 기준만 다르다.
- 429-spillover   : primary 가 429 를 돌려주면 바꾼다. retry-after 동안 그 리전을 막는다.
- capacity-aware  : 추정 사용량에 이번 요청을 더해 용량을 넘으면, 429 전에 바꾼다.

모든 리전이 막혔으면 가장 먼저 풀리는 리전을 새 primary 로 삼아 그때까지 기다린다.
"""

import logging
import threading
import time
from dataclasses import dataclass, field
from types import MappingProxyType

log = logging.getLogger("multi-region")

STATUS_OK = 200
STATUS_TOO_MANY_REQUESTS = 429
# 같은 리전에 다시 보낼 상태 코드. None 은 응답이 없는 경우(연결 실패·타임아웃)다.
# 리전을 옮기는 것은 429 뿐이다. 404·5xx 한 번으로 멀쩡한 리전을 버리면, 그 리전의
# 용량을 통째로 놀리게 된다. 요청 자체의 문제(400·401·403)는 어느 리전에서도 같다.
RETRY_SAME_REGION_STATUSES = frozenset({None, 404, 408, 500, 502, 503, 504})

# 429 에 retry-after 가 없을 때 그 리전을 막아 둘 시간.
DEFAULT_THROTTLE_SECONDS = 1.0
# 모든 리전이 막혔을 때 한 번에 기다리는 최대 시간.
MAX_WAIT_SECONDS = 60.0
# 기다리는 동안 이 간격마다 다시 고른다. 그 사이 다른 요청이 끝나 여유가 생기거나
# 추정치가 바뀔 수 있고, 같은 시각에 잠든 스레드가 한꺼번에 깨어나는 것도 줄인다.
RECHECK_SECONDS = 1.0


@dataclass(frozen=True)
class CallResult:
    status: int | None
    headers: MappingProxyType | dict = field(default_factory=dict)
    usage: dict | None = None
    error: str | None = None
    detail: str | None = None  # 429 가 아닌 실패의 응답 본문(앞부분)


@dataclass(frozen=True)
class Choice:
    region: str
    wait: float


@dataclass(frozen=True)
class Attempt:
    """시도 하나의 기록. 시각은 절대 UTC epoch 초다.

    묶음을 벽시계 위에 놓으려면 실행 시작 시각만으로는 부족하다. 시도마다 보낸 시각을
    남겨야 Azure Monitor 의 분 단위 값과 정렬 없이 맞출 수 있다.
    """

    region: str
    status: int | None
    sent_at: float
    done_at: float
    estimate: float


@dataclass(frozen=True)
class RouteOutcome:
    succeeded: bool
    region: str | None
    attempts: int
    status: int | None
    attempt_log: tuple = ()


def retry_after_seconds(headers):
    """retry-after-ms 를 우선 쓰고, 없으면 retry-after(초). 숫자가 아니면 None."""
    lowered = {key.lower(): value for key, value in headers.items()}
    for name, divisor in (("retry-after-ms", 1000.0), ("retry-after", 1.0)):
        raw = lowered.get(name)
        if not raw:
            continue
        try:
            return float(raw) / divisor
        except (TypeError, ValueError):
            log.warning("%s 헤더를 숫자로 읽지 못했다: %r", name, raw)
    return None


def _earliest(order, seconds_of):
    """가장 먼저 풀리는 리전. 동률이면 우선순위가 높은 리전."""
    region = min(order, key=seconds_of)
    return Choice(region, seconds_of(region))


class Primary:
    """지금 요청을 받는 리전. 막히면 다음 리전으로 옮기고 그대로 둔다."""

    def __init__(self, order):
        self.order = tuple(order)
        if not self.order:
            raise ValueError("리전 순서가 비어 있다")
        self._current = self.order[0]
        self._lock = threading.Lock()

    @property
    def current(self):
        with self._lock:
            return self._current

    def switch_to(self, region):
        if region not in self.order:
            raise ValueError(f"순서에 없는 리전이다: {region!r}")
        with self._lock:
            if region == self._current:
                return
            log.info("primary 전환: %s -> %s", self._current, region)
            self._current = region

    def rotation(self):
        """현재 primary 부터 시작하는 순환 순서."""
        index = self.order.index(self.current)
        return self.order[index:] + self.order[:index]


def _pick_sticky(primary, is_usable, seconds_until):
    """primary 가 쓸 수 있으면 그대로, 아니면 순환 순서에서 쓸 수 있는 리전으로 옮긴다."""
    for region in primary.rotation():
        if is_usable(region):
            primary.switch_to(region)
            return Choice(region, 0.0)
    choice = _earliest(primary.order, seconds_until)
    primary.switch_to(choice.region)
    return choice


def pickers_for(primary):
    """전략 이름 -> pick(tracker, order, estimate). 두 전략이 primary 를 공유하지는 않는다."""

    def pick_429_spillover(tracker, order, estimate):
        return _pick_sticky(primary,
                            lambda region: tracker.blocked_for(region) == 0,
                            tracker.blocked_for)

    def pick_capacity_aware(tracker, order, estimate):
        return _pick_sticky(primary,
                            lambda region: tracker.has_room(region, estimate),
                            lambda region: tracker.seconds_until_room(region, estimate))

    return MappingProxyType({
        "429-spillover": pick_429_spillover,
        "capacity-aware": pick_capacity_aware,
    })


STRATEGIES = ("429-spillover", "capacity-aware")


def _record_success(tracker, estimator, region, estimate, result):
    if result.usage is None:
        # usage 가 없으면 보정할 수 없으니 추정치를 그대로 둔다.
        log.warning("%s: 응답에 usage 가 없어 추정치 %.0f 를 유지한다", region, estimate)
        return
    actual = estimator.actual_cost(result.usage)
    tracker.settle(region, estimate, actual)
    estimator.observe(actual)


def _record_failure(tracker, region, result):
    """실패를 트래커에 반영한다. 다시 보낼 실패면 True.

    429 만 그 리전을 막는다. picker 가 막힌 리전을 보고 primary 를 옮긴다.
    그 외 실패는 막지 않으므로 같은 리전으로 다시 간다.
    """
    if result.status == STATUS_TOO_MANY_REQUESTS:
        seconds = retry_after_seconds(result.headers)
        tracker.mark_throttled(region, DEFAULT_THROTTLE_SECONDS if seconds is None else seconds)
        return True
    return result.status in RETRY_SAME_REGION_STATUSES


def _claim(tracker, order, estimator, pick, sleep, label):
    """보낼 리전을 고르고 예약까지 한 번에 한다. (리전, 예약 전 사용률, 예약량) 을 돌려준다.

    고르기와 예약 사이에 다른 스레드가 끼면 둘 다 "여유 있음"을 보고 같은 리전을
    넘치게 채운다. 기다리는 동안 다른 요청의 응답으로 추정치가 바뀔 수 있으므로
    고를 때마다 추정치를 다시 읽는다. 기다림이 MAX_WAIT_SECONDS 를 넘으면 더
    기다리지 않고 보내 서비스가 판단하게 한다.
    """
    waited = 0.0
    while True:
        with tracker.atomic():
            estimate = estimator.estimate()
            choice = pick(tracker, order, estimate)
            if choice.wait <= 0 or waited >= MAX_WAIT_SECONDS:
                utilization = tracker.utilization(choice.region)
                tracker.reserve(choice.region, estimate)
                return choice.region, utilization, estimate
        wait = min(choice.wait, RECHECK_SECONDS, MAX_WAIT_SECONDS - waited)
        log.debug("%s: 여유 있는 리전이 없어 %.2f초 뒤 다시 고른다 (먼저 풀리는 리전 %s, %.1f초 뒤)",
                  label, wait, choice.region, choice.wait)
        sleep(wait)
        waited += wait


def route_request(send, order, tracker, estimator, pick, max_attempts, sleep, label="",
                  wall_clock=time.time):
    """요청 하나를 성공하거나 max_attempts 에 닿을 때까지 리전을 옮겨 가며 보낸다.

    send(region) -> CallResult
    pick(tracker, order, estimate) -> Choice
    wall_clock() -> 절대 UTC epoch 초. 시도 기록의 시각에만 쓴다.
    """
    result = None
    attempt_log = []

    for attempt in range(1, max_attempts + 1):
        region, utilization, estimate = _claim(tracker, order, estimator, pick, sleep, label)
        log.info("%s 시도 %s -> %s (추정 사용률 %.0f%%, 예약 %.0f)",
                 label, attempt, region, utilization * 100, estimate)
        sent_at = wall_clock()
        result = send(region)
        attempt_log.append(Attempt(region, result.status, sent_at, wall_clock(), estimate))

        if result.status == STATUS_OK:
            _record_success(tracker, estimator, region, estimate, result)
            log.info("%s 시도 %s <- %s HTTP 200 (x-ms-region %s)", label, attempt, region,
                     _header(result.headers, "x-ms-region"))
            return RouteOutcome(True, region, attempt, result.status, tuple(attempt_log))

        tracker.release(region, estimate)
        log.warning("%s 시도 %s <- %s HTTP %s %s", label, attempt, region,
                    result.status, result.error or "")
        if not _record_failure(tracker, region, result):
            return RouteOutcome(False, region, attempt, result.status, tuple(attempt_log))

    return RouteOutcome(False, None, max_attempts,
                        result.status if result else None, tuple(attempt_log))


def _header(headers, name):
    lowered = {key.lower(): value for key, value in headers.items()}
    return lowered.get(name, "-")
