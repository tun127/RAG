"""Product v3 boundary. Flat Agent judgments are never product findings.

This module is deliberately separate from routing: callers must supply the saved
first request and run, and must persist the returned audit before displaying it.
"""
from copy import deepcopy
from threading import RLock
from time import monotonic

from production_rag.pilot_review.findings_v2 import validate_v3


class ProductPool:
    """SharedPool over one product RunBudget; protect all first slots up front.

    All consumers must use this coordinator, rather than mutate RunBudget directly.
    Unknown output is charged conservatively and remains separately observable.
    """

    def __init__(self, budget, request_limit, first_caps, agent_limit=None):
        self.budget = budget
        self.request_limit = request_limit
        self.first = dict(first_caps)
        self.first_limit = sum(self.first.values())
        self.lock = RLock()
        self.held_requests = 0
        self.held_tokens = 0
        self.unknown_output = 0
        self.ledger = []
        self.agent_limit = budget.max_output_tokens if agent_limit is None else agent_limit
        self.agent_spent = 0
        if sum(self.first.values()) > budget.max_output_tokens:
            raise ValueError("first_reservation_exceeds_total")

    def hold(self, requests, tokens):
        with self.lock:
            if requests < 0 or tokens < 0:
                raise ValueError("negative_reservation")
            self.budget.remaining_seconds()
            if (self.agent_spent + self.held_tokens + tokens > self.agent_limit or
                    self.budget.model_calls + self.held_requests + len(self.first) + requests
                    > self.request_limit or
                    self.budget.output_tokens + self.held_tokens + sum(self.first.values())
                    + tokens > self.budget.max_output_tokens):
                return False
            self.held_requests += requests
            self.held_tokens += tokens
            return True

    def finish(self, held_requests, held_tokens, actual_requests, actual_tokens):
        with self.lock:
            if not (0 <= held_requests <= self.held_requests
                    and 0 <= held_tokens <= self.held_tokens
                    and actual_requests >= 0 and actual_tokens >= 0):
                raise ValueError("invalid_settlement")
            self.held_requests -= held_requests
            self.held_tokens -= held_tokens
            self.budget.model_calls += actual_requests
            self.budget.output_tokens += actual_tokens
            self.agent_spent += actual_tokens
            self.ledger.append(dict(stage="agent", requests=actual_requests,
                                    output_tokens=actual_tokens))

    def finish_review(self, held_requests, held_tokens, ledger):
        model = [e for e in ledger if e['stage'] != 'search']
        with self.lock:
            self.finish(held_requests, held_tokens, len(model), sum(e['tokens'] for e in model))
            self.budget.input_tokens += sum(e.get('input_tokens', 0) for e in model)
            self.unknown_output += sum(e['tokens'] for e in model if not e.get('usage_known'))
            self.ledger[-1]['stages'] = deepcopy(ledger)

    def finish_first(self, task_id, output_tokens, input_tokens=0):
        with self.lock:
            cap = self.first[task_id]
            unknown = output_tokens is None
            charged = cap if unknown else output_tokens
            if charged < 0 or input_tokens < 0:
                raise ValueError("negative_usage")
            del self.first[task_id]
            self.budget.charge(dict(eval_count=charged, prompt_eval_count=input_tokens))
            self.unknown_output += cap if unknown else 0
            self.ledger.append(dict(stage="first", task_id=task_id,
                                    output_tokens=output_tokens, reserved_unknown=cap if unknown else 0))

    def snapshot(self):
        return dict(total_limit=self.budget.max_output_tokens, usage=self.budget.usage(),
                    first_limit=self.first_limit,
                    known_output_tokens=self.budget.output_tokens-self.unknown_output,
                    accounted_output_tokens=self.budget.output_tokens,
                    elapsed_seconds=monotonic()-self.budget.started,
                    agent_limit=self.agent_limit, agent_spent=self.agent_spent,
                    unknown_output_reserved=self.unknown_output, first_pending=dict(self.first),
                    held_tokens=self.held_tokens, ledger=deepcopy(self.ledger))


class FirstBudget:
    """A reserved first slot, with charging owned exclusively by ProductPool."""
    def __init__(self, pool, task_id, config, client, deadline, clock=monotonic):
        self.pool, self.task_id, self.config, self.client = pool, task_id, config, client
        self.base = pool.budget
        self.deadline, self.clock = deadline, clock
        self.reserved = pool.unknown_output
        self.charged = False

    def remaining_seconds(self):
        from production_rag.domain.errors import BudgetExceeded
        remaining = self.deadline - self.clock()
        if remaining <= 0:
            raise BudgetExceeded('deadline')
        return remaining

    def allowance(self):
        self.remaining_seconds()
        if self.charged or self.task_id not in self.pool.first:
            from production_rag.domain.errors import BudgetExceeded
            raise BudgetExceeded('first_slot_already_consumed')
        return self.pool.first[self.task_id]

    def charge(self, response):
        self.pool.finish_first(self.task_id, response['eval_count'], response.get('prompt_eval_count', 0))
        self.charged = True

    def finish_call(self):
        if not self.charged and self.client.attempts:
            self.pool.finish_first(self.task_id, None)
        self.reserved = self.pool.unknown_output
        self.client.attempts = []


def validate_candidate(run, task, prepared, first_row, raw_candidate):
    """Validate entire business candidate, preserving every part and approval.

    An Agent Result cannot be passed here. Candidate must be original v3 JSON.
    Model semantic review is an additional gate owned by the orchestrator.
    """
    record = dict(first=deepcopy(first_row), candidate_raw=raw_candidate,
                  candidate_validation=None, accepted=False,
                  semantic_review="not_performed", effective=deepcopy(first_row))
    try:
        record["candidate_validation"] = validate_v3(run, task, prepared, raw_candidate)
    except (ValueError, KeyError, TypeError) as exc:
        record["program_error"] = type(exc).__name__
    return record


class EvidenceRegistry:
    """Append-only IDs bound to one run and its fixed source maps."""

    def __init__(self, run_id, snapshots, entries):
        self.run_id = run_id
        self.snapshots = deepcopy(snapshots)
        self.entries = deepcopy(entries)

    def append(self, run_id, snapshots, entries, verify):
        if run_id != self.run_id or snapshots != self.snapshots:
            raise ValueError("evidence_run_or_version_mismatch")
        for key, value in entries.items():
            if key in self.entries and value != self.entries[key]:
                raise ValueError("evidence_id_rebinding")
            if verify(value) is not True:
                raise ValueError("evidence_location_unverified")
        self.entries.update(deepcopy(entries))
