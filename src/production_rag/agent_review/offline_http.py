"""Explicit SIMULATED HTTP fixture. Cases stay outside all model requests."""
import json
from copy import deepcopy
from types import SimpleNamespace

import httpx
from pydantic import SecretStr

from production_rag.e2e_support import compact_fixture_reply

from .business import dimensions


def settings(cap=1024):
    return SimpleNamespace(retries=0, thinking='disabled', timeout=60,
                           max_completion_tokens=cap, model='spark-x2.6',
                           base_url='https://maas-api.cn-huabei-1.xf-yun.com/v2',
                           api_key=SecretStr('SIMULATED-NOT-A-KEY'))


def transport(case='no_doubt', calls=None, target_check=None):
    calls = calls if calls is not None else []

    def respond(request):
        payload = json.loads(request.content)
        calls.append(payload)
        data = json.loads(payload['messages'][1]['content'])
        stage = data.get('stage', 'first')
        if stage == 'first':
            fixture = {'correction': 'deviation', 'approval_unknown': 'approval-unknown',
                       'attachment_unknown': 'attachment-unknown'}.get(case, 'normal')
            output = compact_fixture_reply(data, fixture)
            if case == 'correction' and data['task']['task_id'].endswith('PAY-01'):
                # Deliberately incorrect but structurally valid first response.
                for part in output['comparisons']:
                    if part['part_id'] == 'payment_algorithm':
                        part.update(conclusion='compliant', reason='SIMULATED 首次误判待发现')
                output['conclusion'] = 'compliant'
        elif stage == 'discovery':
            business = data['data']['business_request']
            question = case != 'no_doubt' and data['check_id'].endswith(
                target_check or ('ACC-01' if case == 'attachment_unknown' else 'PAY-01'))
            output = dict(status='completed', limitations=[], questions=[])
            if question:
                output['questions'] = [dict(trigger='unsupported_assertion',
                    question='核对当前比较的事实依据及条件', basis='SIMULATED 发现器意见，待核实',
                    evidence_ids=[next(iter(data['evidence']))], source='contract',
                    snapshot=data['snapshots']['contract'], resolvable=True)]
            assert business['parts']
        elif stage == 'generation':
            output = deepcopy(data['data']['business_first'])
            if case == 'correction':
                for part in output['comparisons']:
                    if part['part_id'] == 'payment_algorithm':
                        part.update(conclusion='deviation', reason='60自然日较政策默认30自然日延长30日')
                output['conclusion'] = 'deviation'
        else:
            if case == 'semantic_failure':
                raise httpx.ReadTimeout('SIMULATED semantic failure')
            candidate = data['data']['candidate']
            ids = list(data['data']['business_request']['evidence_registry'])[:1]
            support = 'unsupported' if case == 'false_positive' else 'unknown' if case == 'semantic_unknown' else 'supported'
            output = dict(status='unknown' if case == 'semantic_unknown' else 'completed',
                          model_coverage_claim=True, dimensions=[
                dict(field=key, text=value, conditions='SIMULATED 条件核查意见',
                     exceptions='SIMULATED 例外核查意见', counterevidence='SIMULATED 反证核查意见',
                     claims=[dict(assertion='SIMULATED 对本字段的模型意见', status=support,
                                  evidence_ids=ids, explanation='SIMULATED，非人工裁定')])
                for key, value in dimensions(candidate).items()])
        return httpx.Response(200, json=dict(choices=[dict(finish_reason='stop',
            message=dict(content=json.dumps(output, ensure_ascii=False)))],
            usage=dict(prompt_tokens=100, completion_tokens=100)))
    return httpx.MockTransport(respond)
