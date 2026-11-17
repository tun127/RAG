import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_contract_parser import docx, paragraph
from test_findings_closeout import make_run

from production_rag.agent_review.server import IntegrationPolicy
from production_rag.e2e import create_app, run_graph
from production_rag.knowledge_baseline.snapshot import build
from production_rag.product_ui.renderer import create_renderer

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope='module')
def knowledge():
    return build(ROOT / 'data/contract-knowledge-candidate', mode='policies15')


@pytest.mark.parametrize('case', ['no_doubt', 'correction', 'approval_unknown',
    'attachment_unknown', 'false_positive', 'semantic_unknown', 'semantic_failure',
    'budget', 'source_violation'])
def test_product_chain(knowledge, tmp_path, case):
    input_case = {'correction': 'deviation', 'approval_unknown': 'approval-unknown',
                  'attachment_unknown': 'attachment-unknown'}.get(case, 'normal')
    run, _ = make_run(knowledge, tmp_path, input_case)
    policy = IntegrationPolicy(strategy='agentic', case=case, extra_output=0 if case == 'budget' else 3072)
    folder, selected = run_graph(run, policy.config(), tmp_path / 'executions', factory=policy.factory)
    first = json.loads((folder / 'report.json').read_bytes())
    assert len(first['results']) == 6
    assert all(r['execution_status'] == 'success' for r in first['results'])
    assert selected['budget']['usage']['model_calls'] >= 6
    assert not selected['budget']['first_pending']
    assert json.loads((folder / 'selected-report.json').read_bytes()) == selected
    audits = selected['agent_reviews']
    if case == 'no_doubt':
        assert all(a['stop_reason'] == 'no_doubt_found' for a in audits)
        assert all(len(a['ledger']) == 1 for a in audits)
    else:
        target = next(a for a in audits if a['task_id'].endswith('ACC-01' if case == 'attachment_unknown' else 'PAY-01'))
        if case == 'correction':
            assert target['label'] == '补查后更正'
            assert target['candidate_v3_validation']['valid']
            assert target['first']['conclusion'] == 'compliant'
            assert target['candidate']['conclusion'] == 'deviation'
            before = {p['part_id']: p for p in target['first']['comparisons']}
            after = {p['part_id']: p for p in target['candidate']['comparisons']}
            assert before['payment_details'] == after['payment_details']
            generation = next(e for e in target['ledger'] if e['stage'] == 'generation')
            semantic = next(e for e in target['ledger'] if e['stage'] == 'semantic')
            assert 'contract_fact, policy_requirement' not in generation['request']['system']
            assert 'exactly five' not in semantic['request']['system']
            assert 'ACTUAL COMPLETE v3' in semantic['request']['system']
        elif case in ('semantic_unknown', 'false_positive'):
            assert target['label'] == '仍有疑点'
            assert target['first_questioned']
        elif case in ('budget', 'semantic_failure', 'source_violation'):
            assert target['label'] == '复核未完成'
        elif case == 'approval_unknown':
            assert target['effective']['conclusion'] == 'deviation'
            assert target['effective']['approval_status'] == 'required_unknown'
        elif case == 'attachment_unknown':
            assert [p['conclusion'] for p in target['effective']['comparisons']] == ['compliant', 'needs_review']


@pytest.mark.parametrize('interrupt', [False, True])
def test_restart_replay_and_partial_recovery(tmp_path, monkeypatch, interrupt):
    from production_rag.adapters.spark_gateway.client import SparkChat
    from production_rag.agent_review import product
    calls = []
    original_chat = SparkChat.chat

    def counted(self, *args, **kwargs):
        calls.append(1)
        return original_chat(self, *args, **kwargs)

    monkeypatch.setattr(SparkChat, 'chat', counted)
    original_write = product.write_new

    def fail_selection(path, value):
        if interrupt and path.name.endswith('-selection.json'):
            raise OSError('SIMULATED interrupted final selection save')
        return original_write(path, value)

    monkeypatch.setattr(product, 'write_new', fail_selection)
    policy = IntegrationPolicy(strategy='agentic', case='correction')

    def app():
        return create_app(tmp_path, model_policy=policy, renderer=create_renderer())

    with TestClient(app()) as client:
        action = re.search(r'action="(/review\?ticket=[^"]+)"', client.get('/').text)[1]
        document = docx(paragraph('SIMULATED 质量验收及结算资料齐备后60自然日付款。'))
        files = {'file': ('SIMULATED.docx', document)}
        response = client.post(action, files=files)
        rid = str(response.url).split('/')[-1]
        before = client.get('/export/'+rid).json()
        total = len(calls)
        assert str(client.post(action, files=files).url).endswith(rid)
        assert len(calls) == total
        if interrupt:
            assert before['overall_status'] == 'failed'
            assert before['report']['agent_reviews'][0]['first_questioned']
            assert before['report']['agent_reviews'][0]['label'] == '复核未完成'
    with TestClient(app()) as restarted:
        after = restarted.get('/export/'+rid).json()
        assert after['report'] == before['report']
        assert restarted.get('/print/'+rid+'?download=1').status_code == 200
        assert restarted.get('/review/'+rid).status_code == 200
        assert len(calls) == total


@pytest.fixture(scope='module')
def saved_first(knowledge, tmp_path_factory):
    path = tmp_path_factory.mktemp('six-part-mapping')
    run, _ = make_run(knowledge, path, 'normal')
    policy = IntegrationPolicy(strategy='rag')
    folder, _ = run_graph(run, policy.config(), path/'executions', factory=policy.factory)
    return run, folder, json.loads((folder/'report.json').read_bytes())


@pytest.mark.parametrize('suffix', ['PAY-01', 'PAY-02', 'ACC-01', 'ACC-02', 'EXC-01', 'EXC-02'])
def test_every_check_full_candidate_and_semantic_mapping(saved_first, tmp_path, suffix):
    from time import monotonic

    from production_rag.agent.budgets import RunBudget
    from production_rag.agent_review.offline_http import settings, transport
    from production_rag.agent_review.product import review_saved
    from production_rag.agent_review.product_boundary import ProductPool
    from production_rag.agent_review.transport import StagedModel
    run, source, first = saved_first
    for path in source.glob('*-request.json'):
        (tmp_path/path.name).write_bytes(path.read_bytes())
    base = RunBudget(600, 7680, output_tokens=600, model_calls=6)
    model = StagedModel(settings(), monotonic()+60, transport=transport('maintain', target_check=suffix))
    selected = review_saved(run, first, tmp_path, ProductPool(base, 36, {}, agent_limit=3072),
                            monotonic()+60, strategy='agentic', model=model)
    target = next(a for a in selected['agent_reviews'] if a['task_id'].endswith(suffix))
    assert target['stop_reason'] == 'candidate_accepted_under_model_review'
    assert target['candidate_v3_validation']['valid']
    expected = {p['part_id'] for p in target['first']['comparisons']}
    assert expected == {p['part_id'] for p in target['candidate']['comparisons']}
    fields = {d['field'] for d in target['semantic_review']['dimensions']}
    assert 'approval_status' in fields
    assert all(part+'.fact' in fields and part+'.suggestion' in fields for part in expected)
