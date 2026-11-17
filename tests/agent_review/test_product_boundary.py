from concurrent.futures import ThreadPoolExecutor

import pytest

from production_rag.agent.budgets import RunBudget
from production_rag.agent_review.product_boundary import EvidenceRegistry, ProductPool


def test_first_capacity_survives_concurrent_agent_reservations():
    budget = RunBudget(timeout=60, max_output_tokens=100)
    pool = ProductPool(budget, 10, {"a": 40, "b": 40})
    with ThreadPoolExecutor(max_workers=8) as threads:
        accepted = list(threads.map(lambda _: pool.hold(1, 20), range(8)))
    assert sum(accepted) == 1
    pool.finish(1, 20, 1, 7)
    pool.finish_first("a", None)
    pool.finish_first("b", 30)
    assert budget.output_tokens == 77
    assert budget.model_calls == 3
    assert pool.unknown_output == 40
    assert pool.hold(1, 23)
    assert not pool.hold(1, 1)


def test_registry_rejects_cross_run_version_and_rebinding_atomically():
    registry = EvidenceRegistry("r1", {"doc": "v1"}, {"e0": {"quote": "abc"}})
    for run, version in (("r2", "v1"), ("r1", "v2")):
        with pytest.raises(ValueError):
            registry.append(run, {"doc": version}, {"e1": {}}, lambda _: True)
    with pytest.raises(ValueError):
        registry.append("r1", {"doc": "v1"}, {"e0": {"quote": "changed"}}, lambda _: True)
    with pytest.raises(ValueError):
        registry.append("r1", {"doc": "v1"}, {"e1": {}}, lambda _: False)
    assert registry.entries == {"e0": {"quote": "abc"}}
    registry.append("r1", {"doc": "v1"}, {"e1": {"quote": "def"}}, lambda _: True)
    assert registry.entries["e0"] == {"quote": "abc"}
