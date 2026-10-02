"""Azure Monitor 의 분 단위 값이 어떤 창을 재는지 되찾는다.

1차 실험이 남긴 질문은 "이 지표가 순간값 게이지인가, 그 분에 처리한 양인가" 였다.
둘은 사실 같은 식의 특수한 경우다. 지표가 어떤 시각 s 의 직전 width 초 동안 처리한
양이라면, width=60 이고 s 가 분의 끝이면 '그 분의 처리량' 과 완전히 같아진다.

그래서 물어야 할 것은 둘 중 무엇이냐가 아니라 (창 길이 width, 분 시작으로부터 표본을
뜬 위상 phase) 가 얼마냐다. 60분 실행은 표본이 60개고 primary 전환 램프가 여러 번
들어오므로, 램프가 올라가고 내려가는 데 걸리는 시간이 width 를 드러낸다.

입력 점은 (리전, 분 버킷 시작 epoch 초, 관측값 %) 이고, 예측은 클라이언트가 기록한
'완료된 작업' 에서 만든다.
"""

from bisect import bisect_right
from dataclasses import dataclass
from math import sqrt

SECONDS_PER_MINUTE = 60.0
# 램프 길이를 가려내기에 충분히 성기고, 그리드 탐색이 눈에 띄게 느려지지 않을 만큼 좁다.
DEFAULT_WIDTHS = (5.0, 10.0, 15.0, 20.0, 30.0, 45.0, 60.0, 90.0, 120.0, 180.0)
DEFAULT_PHASE_STEP = 1.0
# 분 버킷 시작을 기준으로 표본을 뜬 시각이 있을 수 있는 구간.
DEFAULT_PHASE_RANGE = (0.0, 60.0)


class Series:
    """(끝난 시각, 정규화 토큰) 목록. 구간 합을 누적합 + 이분 탐색으로 준다."""

    def __init__(self, events):
        ordered = sorted(events)
        self._times = [at for at, _ in ordered]
        self._cumulative = [0.0]
        for _, cost in ordered:
            self._cumulative.append(self._cumulative[-1] + cost)

    def __len__(self):
        return len(self._times)

    def tokens(self, low, high):
        """(low, high] 안에서 끝난 작업의 합."""
        if high <= low:
            return 0.0
        start = bisect_right(self._times, low)
        end = bisect_right(self._times, high)
        return self._cumulative[end] - self._cumulative[start]

    def load(self, end, width, capacity_tpm):
        """(end-width, end] 의 처리 속도를 '분당 용량 대비 %' 로."""
        if width <= 0:
            raise ValueError("width 는 0 보다 커야 한다")
        if capacity_tpm <= 0:
            raise ValueError("capacity_tpm 은 0 보다 커야 한다")
        per_window = capacity_tpm * width / SECONDS_PER_MINUTE
        return 100.0 * self.tokens(end - width, end) / per_window


def as_series(events):
    return events if isinstance(events, Series) else Series(events)


def window_load(events, end, width, capacity_tpm):
    return as_series(events).load(end, width, capacity_tpm)


@dataclass(frozen=True)
class Fit:
    """한 (width, phase) 조합의 적합 결과."""

    width: float
    phase: float
    rmse: float
    n: int


def phases(step=DEFAULT_PHASE_STEP, span=DEFAULT_PHASE_RANGE):
    if step <= 0:
        raise ValueError("phase_step 은 0 보다 커야 한다")
    low, high = span
    count = int(round((high - low) / step))
    return tuple(low + step * index for index in range(count + 1))


def split_points(points):
    """(맞추는 절반, 재는 절반). 번갈아 나눠 두 쪽이 실행 전체에 걸치게 한다.

    (width, phase) 는 610가지 후보에서 고른 값이라, 고를 때 쓴 점으로 잰 RMSE 는 낙관적이다.
    고르는 데 쓰지 않은 점에서 다시 재야 그 값을 믿을 수 있다.
    """
    ordered = sorted(points, key=lambda point: (point[1], point[0]))
    return tuple(ordered[0::2]), tuple(ordered[1::2])


def rmse_at(points, series, capacity, width, phase):
    """정해진 (width, phase) 로 이 점들에서 잰 RMSE. 점이 없으면 None."""
    if not points:
        return None
    prepared = {name: as_series(events) for name, events in series.items()}
    return _rmse(points, prepared, capacity, width, phase)


def _rmse(points, series, capacity, width, phase):
    total = 0.0
    for name, stamp, observed in points:
        predicted = series[name].load(stamp + phase, width, capacity[name])
        total += (predicted - observed) ** 2
    return sqrt(total / len(points))


def _best_phase(points, series, capacity, width, candidates):
    best = None
    for phase in candidates:
        error = _rmse(points, series, capacity, width, phase)
        if best is None or error < best.rmse:
            best = Fit(width=width, phase=phase, rmse=error, n=len(points))
    return best


def width_profile(points, series, capacity, widths=DEFAULT_WIDTHS,
                  phase_step=DEFAULT_PHASE_STEP, phase_span=DEFAULT_PHASE_RANGE):
    """창 길이마다 가장 잘 맞는 위상과 그때의 RMSE. widths 순서를 그대로 지킨다."""
    if not points:
        return ()
    prepared = {name: as_series(events) for name, events in series.items()}
    candidates = phases(phase_step, phase_span)
    return tuple(_best_phase(points, prepared, capacity, width, candidates)
                 for width in widths)


def fit(points, series, capacity, widths=DEFAULT_WIDTHS,
        phase_step=DEFAULT_PHASE_STEP, phase_span=DEFAULT_PHASE_RANGE):
    """가장 RMSE 가 작은 (width, phase). 점이 없으면 None."""
    profile = width_profile(points, series, capacity, widths, phase_step, phase_span)
    if not profile:
        return None
    return min(profile, key=lambda row: row.rmse)
