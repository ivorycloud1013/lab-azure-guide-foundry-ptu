"""리전별 PTU 사용량을 클라이언트에서 추정한다.

서비스의 leaky bucket 을 흉내 낸다.
- 요청을 보낼 때 추정치만큼 채우고, 응답을 받으면 실제 usage 로 보정한다.
- 버킷은 PTU 수에 비례한 속도로 계속 빠진다.
- 429 를 받으면 retry-after 동안 그 리전을 막고 가득 찬 것으로 본다.

이 프로세스가 보낸 트래픽만 보이므로 다른 클라이언트가 같은 PTU 를 쓰면 실제보다
낮게 추정된다. 그 오차는 429 를 받았을 때 mark_throttled 로 바로잡는다.
"""

import threading
import time
from dataclasses import dataclass, replace

SECONDS_PER_MINUTE = 60.0


@dataclass(frozen=True)
class RegionState:
    level: float = 0.0          # 추정 사용량 (정규화 토큰)
    updated_at: float = 0.0     # level 을 마지막으로 계산한 시각
    blocked_until: float = 0.0  # 이 시각 전에는 보내지 않는다


def drained(state, drain_per_second, now):
    """now 시점까지 빠진 만큼 level 을 줄인 새 상태."""
    elapsed = max(0.0, now - state.updated_at)
    level = max(0.0, state.level - elapsed * drain_per_second)
    return replace(state, level=level, updated_at=now)


class CapacityTracker:
    """capacities: 리전별 분당 정규화 토큰 (= PTU 수 x 모델의 TPM per PTU)."""

    def __init__(self, capacities, headroom=1.0, clock=time.monotonic):
        if not 0 < headroom <= 1:
            raise ValueError("headroom 은 0 초과 1 이하여야 한다")
        self._capacities = dict(capacities)
        self._headroom = headroom
        self._clock = clock
        # 라우터가 "고르고 예약하기"를 한 덩어리로 묶을 수 있게 재진입 락을 쓴다.
        self._lock = threading.RLock()
        now = clock()
        self._states = {region: RegionState(updated_at=now) for region in capacities}

    def atomic(self):
        """with tracker.atomic(): 안의 조회와 예약은 다른 스레드와 섞이지 않는다."""
        return self._lock

    def _rate(self, region):
        return self._capacities[region] / SECONDS_PER_MINUTE

    def _limit(self, region):
        return self._capacities[region] * self._headroom

    def _current(self, region, now):
        return drained(self._states[region], self._rate(region), now)

    def _update(self, region, change):
        """현재 상태를 계산해 change 로 새 상태를 만들고 교체한다."""
        with self._lock:
            now = self._clock()
            self._states[region] = change(self._current(region, now), now)

    def _snapshot(self, region):
        with self._lock:
            now = self._clock()
            return self._current(region, now), now

    def utilization(self, region):
        state, _ = self._snapshot(region)
        return state.level / self._capacities[region]

    def blocked_for(self, region):
        state, now = self._snapshot(region)
        return max(0.0, state.blocked_until - now)

    def has_room(self, region, estimate):
        return self.seconds_until_room(region, estimate) == 0

    def seconds_until_room(self, region, estimate):
        """estimate 를 넣을 자리가 날 때까지 남은 시간.

        서비스는 현재 사용률이 100% 미만이면 요청을 받으므로, 버킷보다 큰 요청이라도
        버킷이 비는 순간에는 받을 수 있다고 본다.
        """
        state, now = self._snapshot(region)
        blocked = max(0.0, state.blocked_until - now)
        overflow = max(0.0, state.level + estimate - self._limit(region))
        until_empty = state.level / self._rate(region)
        return max(blocked, min(overflow / self._rate(region), until_empty))

    def reserve(self, region, estimate):
        self._update(region, lambda s, _: replace(s, level=s.level + estimate))

    def settle(self, region, estimate, actual):
        self._update(region, lambda s, _: replace(s, level=max(0.0, s.level + actual - estimate)))

    def release(self, region, estimate):
        self.settle(region, estimate, actual=0.0)

    def mark_throttled(self, region, seconds):
        """서비스가 100% 라고 알려왔다. 추정치를 가득 참으로 맞추고 막는다."""
        capacity = self._capacities[region]
        self._update(region, lambda s, now: replace(
            s, level=max(s.level, capacity),
            blocked_until=max(s.blocked_until, now + seconds)))

    def mark_unavailable(self, region, seconds):
        """연결 실패·5xx. 용량과 무관하므로 level 은 그대로 두고 막기만 한다."""
        self._update(region, lambda s, now: replace(
            s, blocked_until=max(s.blocked_until, now + seconds)))
