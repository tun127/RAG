"""SIMULATED single-task orchestration; no production citation protocol."""
from copy import deepcopy
from dataclasses import dataclass
from time import monotonic
from typing import Callable, TypedDict

from langgraph.graph import END, START, StateGraph


@dataclass(frozen=True)
class Scope:
    task: str
    contract: str
    input_snapshot: str
    rule_snapshot: str


@dataclass(frozen=True)
class Request:
    tool: str
    key: str  # Opaque identifier in the caller's allowlist, never a URL/path.


@dataclass(frozen=True)
class Evidence:
    scope: Scope
    source: str
    text: str
    missing: bool = False


@dataclass(frozen=True)
class Decision:
    action: str  # lookup, candidate, human, abstain
    reason: str
    request: Request | None = None
    candidate: str | None = None


class State(TypedDict):
    scope: Scope
    evidence: list[Evidence]
    pending: list[str]
    calls: list[dict]
    trace: list[dict]
    steps: int
    max_lookups: int
    max_decisions: int
    deadline: float
    status: str
    stop_reason: str | None
    candidate: str | None
    validated_sources: list[str]
    decision: Decision | None


@dataclass
class Ports:
    decide: Callable[[State], Decision]
    query_rules: Callable[[Scope, str, float], list[Evidence]]
    read_blocks: Callable[[Scope, str, float], list[Evidence]]
    materials: Callable[[Scope, str, float], list[Evidence]]
    validate: Callable[[Scope, Evidence], bool]
    allowed: dict[str, frozenset[str]]
    clock: Callable[[], float] = monotonic


def initial(scope, evidence=(), *, max_lookups=4, max_decisions=8, seconds=5,
            clock=monotonic):
    if max_lookups < 0 or max_decisions < 1 or seconds <= 0:
        raise ValueError("invalid budget")
    return State(scope=scope, evidence=list(evidence), pending=[], calls=[], trace=[],
                 steps=0, max_lookups=max_lookups, max_decisions=max_decisions,
                 deadline=clock() + seconds, status="running", stop_reason=None,
                 candidate=None, validated_sources=[], decision=None)


def build_graph(ports: Ports):
    """Callbacks are trusted read-only adapters; text is never interpreted as instructions."""
    def stop(s, status, reason):
        s.update(status=status, stop_reason=reason, candidate=None)
        return s

    def guard(s):
        if ports.clock() >= s["deadline"]:
            stop(s, "failed", "time_budget")
            return False
        return True

    def check(s):
        s = deepcopy(s)
        s["trace"].append({"node": "evidence_check", "sources":
                           [e.source for e in s["evidence"]]})
        if not guard(s):
            return s
        if any(e.scope != s["scope"] or not e.source for e in s["evidence"]):
            return stop(s, "failed", "snapshot_or_scope_mismatch")
        if s["steps"] >= s["max_decisions"]:
            return stop(s, "failed", "decision_budget")
        s["steps"] += 1
        try:
            d = ports.decide(deepcopy(s))
            if not guard(s):
                return s
            if not isinstance(d, Decision):
                return stop(s, "failed", "invalid_decision")
            s["decision"] = d
            s["pending"] = [d.reason] if d.action == "lookup" else []
            if d.action == "lookup":
                return s
            if d.action == "human":
                return stop(s, "insufficient", d.reason)
            if d.action == "abstain":
                return stop(s, "abstained", d.reason)
            if d.action != "candidate" or d.candidate not in {
                "compliant", "deviation", "not_applicable"
            }:
                return stop(s, "failed", "invalid_decision")
            # Only the injected validator may approve evidence. No business citation schema.
            if not s["evidence"] or any(e.missing for e in s["evidence"]):
                return stop(s, "insufficient", "missing_evidence")
            if not all(ports.validate(s["scope"], e) for e in s["evidence"]):
                return stop(s, "failed", "citation_validation")
            if not guard(s):
                return s
            s.update(status="candidate", candidate=d.candidate,
                     validated_sources=[e.source for e in s["evidence"]],
                     stop_reason="candidate_ready")
            return s
        except Exception:
            return stop(s, "failed", "decision_or_validation_exception")

    def lookup(s):
        s = deepcopy(s)
        if not guard(s):
            return s
        r = s["decision"].request
        if (not isinstance(r, Request) or
                r.tool not in {"query_rules", "read_blocks", "materials"} or
                r.key not in ports.allowed.get(r.tool, frozenset())):
            return stop(s, "failed", "request_out_of_scope")
        if any(c["tool"] == r.tool and c["key"] == r.key for c in s["calls"]):
            return stop(s, "insufficient", "no_progress_duplicate")
        if len(s["calls"]) >= s["max_lookups"]:
            return stop(s, "failed", "lookup_budget")
        call = {"tool": r.tool, "key": r.key, "sources": [], "outcome": "started"}
        s["calls"].append(call)
        s["trace"].append({"node": "readonly_tool", "tool": r.tool, "key": r.key})
        try:
            result = getattr(ports, r.tool)(s["scope"], r.key,
                                          s["deadline"] - ports.clock())
            if not guard(s):
                call["outcome"] = "time_budget"
                return s
            if not isinstance(result, list) or any(
                not isinstance(e, Evidence) or e.scope != s["scope"] or not e.source
                for e in result
            ):
                call["outcome"] = "snapshot_or_scope_mismatch"
                return stop(s, "failed", call["outcome"])
            call.update(sources=[e.source for e in result], outcome="returned")
            if not result:
                return stop(s, "insufficient", "no_progress_empty")
            s["evidence"].extend(result)
            if any(e.missing for e in result):
                return stop(s, "insufficient", "required_material_missing")
            return s
        except Exception:
            call["outcome"] = "tool_exception"
            return stop(s, "failed", "tool_exception")

    def finish(s):
        return {"trace": s["trace"] + [{"node": "finish", "status": s["status"],
                                      "stop_reason": s["stop_reason"]}]}

    graph = StateGraph(State)
    graph.add_node("evidence_check", check)
    graph.add_node("readonly_tool", lookup)
    graph.add_node("finish", finish)
    graph.add_edge(START, "evidence_check")
    graph.add_conditional_edges("evidence_check", lambda s:
                                "readonly_tool" if s["status"] == "running" else "finish")
    graph.add_conditional_edges("readonly_tool", lambda s:
                                "evidence_check" if s["status"] == "running" else "finish")
    graph.add_edge("finish", END)
    return graph.compile()
