from dataclasses import replace

import pytest

from production_rag.agent_review.demo import scenario
from production_rag.agent_review.engine import Evidence, Reply, review


@pytest.mark.parametrize('field', ['contract_snapshot', 'policy_snapshot', 'check_id', 'criterion'])
@pytest.mark.parametrize('value', ['', '   '])
def test_empty_binding_has_no_effective_result(field, value):
    item, ports, saved = scenario('exception_correction')
    item = replace(item, **{field: value})
    r = review(item, ports)
    assert r['first'] == item.first
    assert r['effective'] is None
    assert r['stop_reason'] == 'invalid_input'
    assert r['input_errors']
    assert r['requests'] == []
    assert saved[0]['effective'] is None
    assert saved[0]['input_errors'] == r['input_errors']


@pytest.mark.parametrize('source,snapshot', [('contract', 'other-contract'),
                                           ('policy', 'other-policy'),
                                           ('external', 'policy-sha256-demo')])
@pytest.mark.parametrize('target', ['initial', 'first'])
def test_source_binding_cannot_be_bypassed_by_valid_boolean(source, snapshot, target):
    item, ports, saved = scenario('exception_correction')
    bad = Evidence(source, snapshot, 'x', 'quote')
    if target == 'initial':
        item = replace(item, evidence=item.evidence + (bad,))
    else:
        item = replace(item, first=replace(item.first, citations=(bad,)))
    assert item.first_validation.valid
    r = review(item, ports)
    assert r['effective'] is None
    assert r['stop_reason'] == 'invalid_input'
    assert r['usage']['requests'] == 0
    assert saved and r['input_errors']


def test_first_citation_must_belong_to_initial_evidence():
    item, ports, _ = scenario('exception_correction')
    item = replace(item, first=replace(item.first, citations=(
        Evidence('contract', item.contract_snapshot, 'other', 'not supplied'),)))
    r = review(item, ports)
    assert 'first_citation_not_in_initial_evidence' in r['input_errors']
    assert r['effective'] is None


def test_coverage_partition_preserves_duplicate_questions():
    item, ports, _ = scenario('exception_correction')
    item = replace(item, questions=item.questions * 4)
    payloads = []
    def generate(payload):
        payloads.append(payload)
        return Reply(item.first)
    ports.generate = generate
    r = review(item, ports)
    assert [x['index'] for x in r['checked_questions']] == [0, 1]
    assert [x['index'] for x in r['uncovered_questions']] == [2, 3]
    assert all(x['reason'] == 'search_limit' for x in r['uncovered_questions'])
    assert 'questions' not in payloads[0]
    assert payloads[0]['uncovered_questions']


def test_failed_search_is_uncovered():
    item, ports, _ = scenario('retrieval_failure')
    r = review(item, ports)
    assert r['checked_questions'] == []
    assert r['uncovered_questions'][0]['reason'] == 'external_call_failed'


def test_important_changes_with_same_judgment():
    item, ports, _ = scenario('exception_correction')
    candidate = replace(item.first, contract_fact='corrected fact',
                        approval_status='unknown', reason='corrected reason', citations=())
    ports.generate = lambda _: Reply(candidate)
    r = review(item, ports)
    assert not r['changed']
    assert r['any_content_changed']
    assert r['field_changes']['contract_fact']
    assert r['field_changes']['reason']
    assert r['field_changes']['citations']
    assert not r['field_changes']['approval_status']


def test_semantic_unknown_never_promotes_candidate():
    from production_rag.agent_review.engine import Validation
    item, ports, _ = scenario('exception_correction')
    ports.validate = lambda *args: Validation(True, True, None, 'unknown')
    r = review(item, ports)
    assert r['effective'] == item.first
    assert r['stop_reason'] == 'candidate_rejected'
    assert not r['any_content_changed']
