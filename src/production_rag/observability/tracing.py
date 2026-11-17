from time import monotonic

from production_rag.domain.models import TraceEvent


class Trace:
    """只记录可见决策、工具输入输出和预算，不索取或保存模型隐藏思维。"""

    def __init__(self):
        self.started = monotonic()
        self.events: list[TraceEvent] = []

    @property
    def elapsed_ms(self) -> float:
        return round((monotonic() - self.started) * 1000, 2)

    def add(self, stage: str, **data):
        self.events.append(
            TraceEvent(
                sequence=len(self.events) + 1, stage=stage, elapsed_ms=self.elapsed_ms, data=data
            )
        )
