"""教学入口 2：所有模型调用共用同一个预算，而非每一步重新计时。"""

from dataclasses import dataclass, field
from time import monotonic

from production_rag.domain.errors import BudgetExceeded


@dataclass
class RunBudget:
    timeout: float
    max_output_tokens: int
    started: float = field(default_factory=monotonic)
    output_tokens: int = 0
    input_tokens: int = 0
    model_calls: int = 0
    per_call_cap: int = 768

    def remaining_seconds(self) -> float:
        remaining = self.timeout - (monotonic() - self.started)
        if remaining <= 0:
            raise BudgetExceeded("deadline")
        return remaining

    def allowance(self) -> int:
        self.remaining_seconds()
        remaining = self.max_output_tokens - self.output_tokens
        if remaining <= 0:
            raise BudgetExceeded("output_tokens")
        return min(self.per_call_cap, remaining)

    def charge(self, response: dict):
        self.model_calls += 1
        self.output_tokens += response.get("eval_count", 0)
        self.input_tokens += response.get("prompt_eval_count", 0)

    def usage(self) -> dict[str, int]:
        return {
            "model_calls": self.model_calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
        }
