"""Stateless staged adapter using the product's Spark HTTP/error boundary."""
import json
from time import monotonic

from production_rag.adapters.spark_gateway.client import SparkChat
from production_rag.agent.budgets import RunBudget

from .engine import Reply


class StagedModel:
    def __init__(self, settings, deadline, *, transport=None, clock=monotonic):
        if settings.retries != 0 or settings.thinking != "disabled":
            raise ValueError("agent_requires_zero_retries_disabled_thinking")
        self.settings = settings
        self.deadline = deadline
        self.transport = transport
        self.clock = clock
        self.ledger = []

    def __call__(self, request):
        if request["stage"] not in ("discovery", "generation", "semantic"):
            raise ValueError("unknown_model_stage")
        remaining = self.deadline - self.clock()
        if remaining <= 0:
            raise TimeoutError("shared_deadline")
        cap = request["max_tokens"]
        budget = RunBudget(timeout=remaining, max_output_tokens=cap, per_call_cap=cap)
        client = SparkChat(self.settings, transport=self.transport, clock=self.clock)
        event = dict(stage=request["stage"], request=request, raw=None, attempts=[])
        self.ledger.append(event)
        try:
            response = client.chat([
                {"role": "system", "content": request["system"]},
                {"role": "user", "content": json.dumps(
                    {k: v for k, v in request.items() if k != "system"}, ensure_ascii=False)},
            ], budget)
            event["raw"] = response["content"]
            return Reply(response["content"], budget.output_tokens, budget.input_tokens)
        except Exception as exc:
            exc.usage_known = bool(client.attempts) and all(a.get('usage_known') for a in client.attempts)
            exc.output_tokens = budget.output_tokens
            event["error"] = getattr(exc, "kind", type(exc).__name__)
            event["raw"] = getattr(exc, "final_answer", None)
            raise
        finally:
            event["attempts"] = client.attempts
            event["usage"] = budget.usage()
