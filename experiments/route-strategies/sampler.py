"""실행 중 클라이언트가 추정한 리전별 사용률을 주기적으로 찍는다.

리포트의 '버킷 수위' 계열은 tracker 의 내부 상태라 요청 기록만으로는 복원할 수 없다.
Azure Monitor 의 분 단위 값과 견주려면 이 계열도 절대 시각과 함께 남아 있어야 한다.
"""

import threading
import time

DEFAULT_INTERVAL_SECONDS = 1.0


class UtilizationSampler:
    """tracker.utilization(region) 을 interval 마다 기록하는 데몬 스레드."""

    def __init__(self, tracker, regions, interval=DEFAULT_INTERVAL_SECONDS, clock=time.time):
        if interval <= 0:
            raise ValueError("interval 은 0 보다 커야 한다")
        self._tracker = tracker
        self._regions = tuple(regions)
        self._interval = interval
        self._clock = clock
        self._stop = threading.Event()
        self._thread = None
        self._samples = []

    @property
    def samples(self):
        return tuple(self._samples)

    def sample_once(self):
        levels = {name: round(self._tracker.utilization(name) * 100, 2)
                  for name in self._regions}
        entry = {"at": round(self._clock(), 3), "levels": levels}
        self._samples.append(entry)
        return entry

    def start(self):
        if self._thread is not None:
            raise RuntimeError("이미 시작했다")
        self._thread = threading.Thread(target=self._loop, name="sampler", daemon=True)
        self._thread.start()
        return self

    def stop(self, timeout=5.0):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        self.sample_once()          # 마지막 상태를 남긴다
        return self.samples

    def _loop(self):
        while not self._stop.wait(self._interval):
            self.sample_once()
