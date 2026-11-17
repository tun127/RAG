import pytest

from production_rag.agent_review.demo import NAMES, scenario
from production_rag.agent_review.engine import Evidence, Reply, Validation, review


@pytest.mark.parametrize('name', NAMES)
def test_scenarios(name):
    item, ports, saved = scenario(name)
    r = review(item, ports)
    assert r['first'] == item.first
    assert r['usage']['requests'] <= 3
    assert sum(e['kind'] == 'generation' for e in r['requests']) <= 1
    assert saved and r['saved']
    if name in ('correct_no_review', 'unknown_material'):
        assert r['usage']['requests'] == 0
    elif name in ('exception_correction', 'linked_clause', 'unsupported_quote', 'injection'):
        assert r['changed']
    elif name in ('conflict_uncertain', 'no_new_evidence'):
        assert r['effective'].judgment == 'uncertain'
    elif name in ('retrieval_failure', 'budget_stop', 'invalid_candidate'):
        assert r['effective'] == item.first
        assert r['outcome'] == 'review_failed'
    if name == 'invalid_first':
        assert not r['first_validation'].valid
        assert r['candidate_validation'].valid


def test_no_promotion_after_invalid_first_and_invalid_candidate():
    item, ports, _ = scenario('invalid_first')
    ports.validate = lambda *args: Validation(False, False, False)
    r = review(item, ports)
    assert r['effective'] is None


def test_cross_snapshot_rejected():
    item, ports, _ = scenario('exception_correction')
    ports.policy_search = lambda _: Reply((Evidence('policy', 'other-run', 'x', 'x'),))
    r = review(item, ports)
    assert r['stop_reason'] == 'unauthorized_evidence'
    assert r['usage']['requests'] == 1


def test_shared_budget_not_reset():
    item, ports, _ = scenario('exception_correction')
    ports.budget.requests = 2
    assert review(item, ports)['usage']['requests'] == 2
    assert review(item, ports)['stop_reason'] == 'shared_budget_exhausted'


def test_location_is_not_semantic_support():
    item, ports, _ = scenario('exception_correction')
    ports.validate = lambda *args: Validation(True, True, False)
    r = review(item, ports)
    assert r['stop_reason'] == 'candidate_rejected'
    assert r['effective'] == item.first


def test_deadline_checked_after_sync_call():
    item, ports, saved = scenario('exception_correction')
    now = [0]
    ports.clock = lambda: now[0]
    def slow(_):
        now[0] = 100
        return Reply(())
    ports.policy_search = slow
    r = review(item, ports)
    assert r['stop_reason'] == 'deadline_after_call'
    assert saved and r['usage']['requests'] == 1


def test_two_searches_max():
    from dataclasses import replace
    item, ports, _ = scenario('exception_correction')
    item = replace(item, questions=item.questions * 5)
    r = review(item, ports)
    assert sum(e['kind'] == 'search' for e in r['requests']) == 2
