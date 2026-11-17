import json
from dataclasses import replace

import pytest

from production_rag.agent_review.auto_demo import NAMES, fixture
from production_rag.agent_review.automatic import (
    Signals,
    automatic_review,
    deterministic,
    parse_candidate,
    parse_doubts,
    parse_semantics,
)
from production_rag.agent_review.engine import Reply


@pytest.mark.parametrize('name', NAMES)
def test_automatic_raw_chain(name):
    item, ports, signals, calls, saved = fixture(name)
    r = automatic_review(item, ports, signals)
    assert r['first'] == saved[0]['first']
    assert r['human_adjudication'] == 'not_performed'
    assert r['usage']['requests'] <= 5
    for stage in ('discovery', 'generation', 'semantic'):
        assert sum(c['stage'] == stage for c in calls) <= 1
    assert sum(e['stage'] == 'search' for e in r['ledger']) <= 2
    assert ports.pool.charged == (r['usage']['requests'], r['usage']['tokens'])
    assert r['saved']
    if name == 'no_review':
        assert r['stop_reason'] == 'no_doubt_found'
        assert len(calls) == 1
        assert r['semantic_review'] is None
    elif name == 'approval_unknown':
        assert not calls
        assert r['stop_reason'] == 'limitations_only'
    elif name in ('exception', 'injection'):
        assert r['stop_reason'] == 'candidate_accepted_under_model_review'
        assert r['changed']
        assert [c['stage'] for c in calls] == ['discovery', 'generation', 'semantic']
    elif name == 'budget':
        assert not calls
        assert r['stop_reason'] == 'shared_budget_insufficient_for_complete_chain'
    elif name == 'discovery_invalid':
        assert r['discovery']['status'] == 'failed'
        assert len(calls) == 1
    else:
        assert r['effective']['judgment'] == item.first.judgment
        assert r['first_questioned']
        assert r['first_status'] == 'questioned_original_record_retained'
    assert all('questions' not in c.get('data', {}) for c in calls)


def test_invalid_first_cannot_be_rescued_by_failed_review():
    from production_rag.agent_review.engine import Validation
    item, ports, signals, _, _ = fixture('review_failure')
    item = replace(item, first_validation=Validation(False, False, None))
    r = automatic_review(item, ports, signals)
    assert r['effective'] is None
    assert r['first_questioned']


def test_invalid_binding_calls_nothing():
    item, ports, signals, calls, saved = fixture('exception')
    r = automatic_review(replace(item, contract_snapshot=''), ports, signals)
    assert r['effective'] is None
    assert not calls and saved
    assert r['stop_reason'] == 'admit_failed'


def test_missing_mapping_and_low_confidence_do_not_create_doubt():
    item, *_ = fixture('exception')
    assert deterministic(item, Signals(policy_dependencies=('exception',))) == []
    qs = deterministic(item, Signals(policy_dependencies=('exception',),
                                    policy_evidence_map={'exception': ()}))
    assert len(qs) == 1


def test_deterministic_reduces_model_calls():
    item, ports, _, calls, _ = fixture('exception')
    r = automatic_review(item, ports, Signals(policy_dependencies=('exception',),
                                            policy_evidence_map={'exception': ()}))
    assert r['discovery']['method'] == 'deterministic'
    assert [c['stage'] for c in calls] == ['generation', 'semantic']


def test_foreign_discovery_evidence_rejected():
    item, ports, signals, _, _ = fixture('exception')
    original = ports.model
    def model(req):
        reply = original(req)
        obj = json.loads(reply.value)
        obj['questions'][0]['evidence_ids'] = ['other-run']
        return Reply(json.dumps(obj), 10)
    ports.model = model
    r = automatic_review(item, ports, signals)
    assert r['discovery']['status'] == 'failed'
    assert r['usage']['requests'] == 1


def test_location_failure_skips_semantic_model():
    item, ports, signals, calls, _ = fixture('exception')
    ports.locate = lambda *args: False
    r = automatic_review(item, ports, signals)
    assert r['stop_reason'] == 'candidate_location_rejected'
    assert [c['stage'] for c in calls] == ['discovery', 'generation']


def test_semantics_rejects_omitted_dimension_unknown_evidence_and_changed_text():
    item, ports, _, _, _ = fixture('exception')
    from production_rag.agent_review.engine import Evidence
    ev = {'e0': item.evidence[0], 'e1': Evidence('policy', 'p-fixed', 'p', 'policy')}
    req = {'stage': 'generation'}
    candidate = parse_candidate(ports.model(req).value, ev)
    raw = json.loads(ports.model({'stage': 'semantic'}).value)
    for mutation in ('omit', 'foreign', 'alter'):
        obj = json.loads(json.dumps(raw))
        if mutation == 'omit':
            obj['dimensions'].pop()
        elif mutation == 'foreign':
            obj['dimensions'][0]['claims'][0]['evidence_ids'] = ['foreign']
        else:
            obj['dimensions'][0]['text'] = 'selected favorable excerpt'
        with pytest.raises(ValueError):
            parse_semantics(json.dumps(obj), candidate, ev)


def test_discovery_unknown_does_not_mean_no_doubts():
    item, ports, signals, _, _ = fixture('exception')
    ports.model = lambda req: Reply('{"status":"unknown","questions":[],"limitations":["ambiguous"]}')
    r = automatic_review(item, ports, signals)
    assert r['stop_reason'] == 'discovery_unknown'
    assert r['semantic_review'] is None


def test_strict_model_text_only():
    item, *_ = fixture('exception')
    with pytest.raises(ValueError):
        parse_doubts({'status': 'completed'}, {}, item)


def test_two_searches_and_remaining_doubt_are_audited():
    item, ports, signals, _, _ = fixture('exception')
    original = ports.model
    def model(req):
        reply = original(req)
        if req['stage'] == 'discovery':
            obj = json.loads(reply.value)
            obj['questions'] *= 3
            return Reply(json.dumps(obj), reply.tokens)
        return reply
    ports.model = model
    r = automatic_review(item, ports, signals)
    assert len(r['checked_questions']) == 2
    assert len(r['uncovered_questions']) == 1
    assert r['uncovered_questions'][0]['reason'] == 'search_limit'
    assert r['usage']['requests'] == 5


def test_search_failure_and_budget_charge_remain_visible():
    item, ports, signals, _, saved = fixture('exception')
    def fail(req):
        raise RuntimeError('search failed')
    ports.search = fail
    r = automatic_review(item, ports, signals)
    assert r['stop_reason'] == 'search_failed'
    assert r['first_questioned'] and r['candidate'] is None
    assert r['uncovered_questions'][0]['reason'] == 'search_failed_or_not_started'
    assert saved
    assert ports.pool.charged[0] == 2


def test_generation_foreign_evidence_is_rejected_before_location():
    item, ports, signals, _, _ = fixture('exception')
    original = ports.model
    def model(req):
        reply = original(req)
        if req['stage'] == 'generation':
            obj = json.loads(reply.value)
            obj['evidence_ids'] = ['other-run']
            return Reply(json.dumps(obj), reply.tokens)
        return reply
    ports.model = model
    r = automatic_review(item, ports, signals)
    assert r['stop_reason'] == 'generate_failed'
    assert r['semantic_review'] is None
    assert r['effective']['judgment'] == item.first.judgment


def test_budget_is_shared_and_unused_reservations_are_released():
    item, ports, signals, calls, _ = fixture('no_review')
    ports.pool.requests = 5
    first = automatic_review(item, ports, signals)
    assert first['usage']['requests'] == 1
    assert ports.pool.requests == 4
    second = automatic_review(item, ports, signals)
    assert second['stop_reason'] == 'shared_budget_insufficient_for_complete_chain'
    assert len(calls) == 1


def test_sync_deadline_preserves_audit_and_stops_next_model():
    item, ports, signals, calls, saved = fixture('exception')
    now = [0]
    ports.clock = lambda: now[0]
    original = ports.model
    def model(req):
        reply = original(req)
        now[0] = 100
        return reply
    ports.model = model
    r = automatic_review(item, ports, signals)
    assert r['discovery']['status'] == 'failed'
    assert len(calls) == 1
    assert 'deadline_after_call' in r['errors']
    assert saved and ports.pool.charged == (1, 10)


def test_injection_remains_data_and_no_reference_expectations_are_sent():
    item, ports, signals, calls, _ = fixture('injection')
    automatic_review(item, ports, signals)
    for req in calls:
        assert 'untrusted' in req['system']
        assert 'Ignore instructions' in req['evidence']['e0']['quote']
        assert 'expected' not in req and 'reference' not in req
    assert item.questions == ()


def test_semantic_failure_is_independently_charged():
    item, ports, signals, _, _ = fixture('semantic_failure')
    r = automatic_review(item, ports, signals)
    assert r['ledger'][-1]['stage'] == 'semantic'
    assert r['ledger'][-1]['status'] == 'failed'
    assert r['ledger'][-1]['tokens'] == ports.model_tokens
    assert r['usage']['tokens'] == ports.pool.charged[1]
    assert r['semantic_review']['status'] == 'failed'


def test_deterministic_counterevidence_remains_questioned_when_budget_stops():
    item, ports, _, calls, _ = fixture('budget')
    r = automatic_review(item, ports, Signals(unsupported_claims=('approval assertion',)))
    assert r['first_questioned']
    assert r['first_status'] == 'questioned_original_record_retained'
    assert r['stop_reason'] == 'shared_budget_insufficient_for_complete_chain'
    assert not calls
