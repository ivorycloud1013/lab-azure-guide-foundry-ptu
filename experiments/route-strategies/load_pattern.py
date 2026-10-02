"""PTU 용량에 맞춘 부하를 일정 간격의 묶음으로 나눠 보낸다.

동시 요청 수를 고정하면 응답이 느려질수록 보내는 양도 줄어든다. 실제 트래픽처럼
응답 속도와 상관없이 정해진 시각에 정해진 양을 보내야 PTU 포화를 재현할 수 있다.
"""

import math
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

SECONDS_PER_MINUTE = 60.0


@dataclass(frozen=True)
class Batch:
    offset: float  # 시작 시각으로부터 몇 초 뒤에 보낼지
    size: int      # 이 시각에 보낼 요청 수


def requests_per_minute(ptu, tpm_per_ptu, cost_per_request):
    """PTU 가 1분에 처리할 수 있는 요청 수."""
    if ptu <= 0 or tpm_per_ptu <= 0 or cost_per_request <= 0:
        raise ValueError("PTU, TPM per PTU, 요청당 비용은 모두 양수여야 한다")
    return ptu * tpm_per_ptu / cost_per_request


def paced_batches(requests_per_minute, interval_seconds, duration_seconds):
    """duration 동안 interval 마다 보낼 묶음. 소수점은 다음 묶음으로 넘겨 총량을 맞춘다."""
    if requests_per_minute <= 0 or interval_seconds <= 0 or duration_seconds <= 0:
        raise ValueError("요청률, 간격, 기간은 모두 양수여야 한다")
    count = int(duration_seconds // interval_seconds)
    if count == 0:
        raise ValueError("기간이 간격보다 짧다")

    per_batch = requests_per_minute * interval_seconds / SECONDS_PER_MINUTE
    batches = []
    sent = 0
    for index in range(count):
        # round() 는 짝수 쪽으로 반올림하므로 0.5 를 더해 내림한다.
        target = math.floor(per_batch * (index + 1) + 0.5)
        batches.append(Batch(offset=float(index * interval_seconds), size=target - sent))
        sent = target
    return tuple(batches)


def run_paced(batches, run_one, max_workers, sleep=time.sleep, clock=time.monotonic):
    """묶음마다 제 시각에 run_one(index) 를 띄우고, 보낸 순서대로 결과를 돌려준다.

    앞 요청이 끝나기를 기다리지 않는다. 일정보다 늦었으면 쉬지 않고 바로 보낸다.
    """
    futures = []
    with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="w") as pool:
        start = clock()
        for batch in batches:
            delay = start + batch.offset - clock()
            if delay > 0:
                sleep(delay)
            first = len(futures) + 1
            futures.extend(pool.submit(run_one, index)
                           for index in range(first, first + batch.size))
        return [future.result() for future in futures]
