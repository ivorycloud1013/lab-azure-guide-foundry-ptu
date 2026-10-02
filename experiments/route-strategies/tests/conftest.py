import sys
from pathlib import Path

# multi-region/ 의 모듈을 테스트에서 바로 import 한다.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class FakeClock:
    """테스트가 시간을 직접 넘기는 monotonic 시계."""

    def __init__(self, start=1000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds
