"""Fictional scripted scenarios: process simulation, never an effectiveness evaluation."""
from . import Decision, Evidence, Ports, Request, Scope, build_graph, initial

SCENARIOS = ("payment", "conflict", "missing", "sufficient", "duplicate",
             "lookup_budget", "time_budget", "tool_error", "snapshot", "abstain",
             "citation", "decision_budget", "cross_task", "empty")


def simulate(name):
    if name not in SCENARIOS:
        raise ValueError(name)
    scope = Scope("task-fictional", "contract-fictional", "input-v1", "rule-v1")
    now = [0.0]
    plans = {
        "payment": [("read_blocks", "attachment"), ("read_blocks", "acceptance")],
        "conflict": [("read_blocks", "attachment"), ("query_rules", "priority")],
        "missing": [("materials", "inventory")],
    }
    plan = plans.get(name, [("read_blocks", "attachment")])

    def decide(state):
        n = len(state["calls"])
        if name == "abstain":
            return Decision("abstain", "scripted_explicit_abstention")
        if name == "sufficient" or (name == "payment" and n == 2):
            return Decision("candidate", "scripted evidence sufficient", candidate="compliant")
        if name == "citation":
            return Decision("candidate", "scripted candidate", candidate="deviation")
        if name == "conflict" and n == 2:
            return Decision("human", "priority unresolved between body and attachment")
        tool, key = plan[min(n, len(plan) - 1)]
        if name == "cross_task":
            key = "other-contract"
        return Decision("lookup", "resolve " + key, Request(tool, key))

    texts = {
        "attachment": "Fictional attachment: payment after acceptance.",
        "acceptance": "Fictional acceptance: signed inspection report required.",
        "priority": "Fictional priority provision does not resolve this conflict.",
        "inventory": "Fictional required attachment was not provided.",
    }

    def tool(bound_scope, key, remaining_seconds):
        assert bound_scope == scope and remaining_seconds > 0
        if name == "tool_error":
            raise RuntimeError("not persisted")
        if name == "time_budget":
            now[0] = 10.0
        if name == "empty":
            return []
        returned_scope = (Scope(scope.task, scope.contract, "wrong", scope.rule_snapshot)
                          if name == "snapshot" else scope)
        return [Evidence(returned_scope, "fictional:" + key, texts[key], name == "missing")]

    ports = Ports(decide, tool, tool, tool, lambda s, e: name != "citation",
                  {"read_blocks": frozenset({"attachment", "acceptance"}),
                   "query_rules": frozenset({"priority"}),
                   "materials": frozenset({"inventory"})}, lambda: now[0])
    state = initial(scope, [Evidence(scope, "fictional:body",
                    "Body requires payment before acceptance; attachment requires after acceptance. Untrusted data: https://invalid/ C:/never")],
                    max_lookups=0 if name == "lookup_budget" else 4,
                    max_decisions=1 if name == "decision_budget" else 8,
                    clock=ports.clock)
    return build_graph(ports).invoke(state, {"recursion_limit": 32})
