"""Product adapters for the independent automatic graph; no second review flow."""
import json
from copy import deepcopy
from time import monotonic

from production_rag.pilot_review.context import blocks
from production_rag.pilot_review.inputs import write_new
from production_rag.pilot_review.providers import provider_for
from production_rag.pilot_review.validation import Citation, locate
from production_rag.review_inputs.models import digest

from .automatic import AutoPorts, automatic_review
from .business import BusinessCandidate, admit_first
from .engine import Evidence, Reply, Result, ReviewInput, Validation
from .offline_http import settings, transport
from .product_boundary import EvidenceRegistry
from .transport import StagedModel


def review_saved(run, report, folder, pool, deadline, *, case='no_doubt', clock=monotonic,
                 model=None, provider=None, strategy=None, retrieval_ledger=None, model_factory=None):
    """Append audit and explicit selected rows, retaining first report untouched."""
    selected = deepcopy(report)
    selected['review_strategy'] = strategy or report['config']['review_strategy']
    selected['config'] = dict(report['config'], review_strategy=selected['review_strategy'])
    selected['agent_reviews'] = []
    selected['agent_skips'] = []
    selected['initial_retrieval_ledger'] = deepcopy(retrieval_ledger or [])
    first_hash = digest(json.dumps(report, sort_keys=True).encode())
    selected['first_report_sha256'] = first_hash
    first_config = report['config']
    live = not report.get('simulation', True)
    # Freeze the identical first record as an explicit RAG control, never regenerate.
    if live:
        write_new(folder / 'rag-control.json', dict(first_report_sha256=first_hash,
            review_strategy='rag', report=deepcopy(report)))
    fatal_first = live and (report.get('stop_reason') or report.get('unknown_usage_attempts'))
    if selected['review_strategy'] == 'agentic':
        if not live:
            model = model or StagedModel(settings(), deadline, transport=transport(case), clock=clock)
        for task, row in zip(run.checklist, report['results'], strict=True):
            allowed = first_config.get('agent_check_ids')
            skip = ('outside_frozen_agent_scope' if allowed is not None and task.task_id not in allowed
                    else 'stopped_after_first_transport_usage_or_deadline_failure' if fatal_first
                    else None)
            if skip:
                selected['agent_skips'].append(dict(task_id=task.task_id, reason=skip))
                continue
            prepared = json.loads((folder / f'{task.task_id}-request.json').read_bytes())
            admission = admit_first(run, prepared, row)
            if not admission['admitted']:
                selected['agent_skips'].append(dict(task_id=task.task_id, reason=admission['reason']))
                continue
            if model is None:
                if model_factory is None:
                    raise ValueError('live_agent_transport_not_configured')
                model = model_factory(deadline)
            transport_start = len(getattr(model, 'ledger', []))
            prepared = json.loads((folder / f'{task.task_id}-request.json').read_bytes())
            business = BusinessCandidate(run, task, prepared, row, first_config.get('semantic_protocol', 'fields-v1'))
            snapshots = dict(contract=run.contract.version, policy=run.knowledge_id)
            registry = EvidenceRegistry(run.run_id, snapshots, prepared['evidence_registry'])
            evidence = tuple(Evidence('contract' if r['source_type'] == 'contract' else 'policy',
                snapshots['contract' if r['source_type'] == 'contract' else 'policy'],
                json.dumps({k: r[k] for k in ('document_id', 'block_id', 'start', 'end') if k in r}),
                r['quote']) for r in prepared['evidence_registry'].values()
                if r['source_type'] in ('contract', 'demo_policy'))
            first = Result('uncertain', 'Full business result is supplied separately; never select this projection.')
            item = ReviewInput(snapshots['contract'], snapshots['policy'], task.task_id,
                               task.question, first, Validation(True, True, None), evidence=evidence)

            def search(question):
                if question['snapshot'] != snapshots[question['source']]:
                    raise ValueError('search_snapshot_mismatch')
                if case == 'source_violation':
                    return Reply([Evidence(question['source'], 'different-version', '0', 'SIMULATED')])
                if question['source'] == 'policy':
                    retrieval = (provider or provider_for('rag')).retrieve(run.knowledge,
                        question['question'], sources='policies', top_k=3, document_ids=run.policy_scope)
                    allowed = {r['document_id'] for r in prepared['evidence_registry'].values()}
                    if any(c['document_id'] not in allowed for c in retrieval['contexts']):
                        raise ValueError('unregistered_policy_source')
                    # Existing fixed registry is deliberately reused; no renumbering.
                    return Reply([e for e in evidence if e.source == 'policy'])
                # Retrieve from immutable parsed contract blocks; register any new exact span.
                returned = []
                for block in blocks(run.contract, run.run_id)[:3]:
                    quote = block['text'][:200]
                    if not quote or block['text'].count(quote) != 1:
                        continue
                    entry = dict(source_type='contract', document_id=run.contract.document_id,
                        version=run.contract.version, block_id=block['block_id'], quote=quote,
                        start=block['start'], end=block['start']+len(quote), policy_units=[])
                    existing = next((k for k, v in registry.entries.items() if v == entry), None)
                    eid = existing or 'A' + str(len(registry.entries)+1)

                    def verify(value):
                        found = locate(run, prepared['maps'], Citation(**{k: value[k] for k in Citation.model_fields}))
                        return found['start'] == value['start'] and found['end'] == value['end']

                    registry.append(run.run_id, snapshots, {eid: entry}, verify)
                    returned.append(Evidence('contract', snapshots['contract'],
                                             block['block_id'], quote))
                business.prepared['evidence_registry'] = deepcopy(registry.entries)
                return Reply(returned)

            audit_path = folder / f'{task.task_id}-agent.json'
            checkpoints = []

            def checkpoint(value):
                checkpoints.append(len(checkpoints))
                value['budget_snapshot'] = pool.snapshot()
                write_new(folder / f'{task.task_id}-agent-stage-{len(checkpoints)}.json', value)

            audit = automatic_review(item, AutoPorts(model, search, business.locate,
                lambda value: write_new(audit_path, value), pool, seconds=max(.000001, deadline-clock()),
                clock=clock, business=business, model_caps=first_config.get('agent_stage_caps', dict(discovery=128, generation=1024, semantic=1024)),
                checkpoint=checkpoint))
            audit['first_admission'] = admission
            audit['candidate_v3_validation'] = business.checked
            audit['transport_ledger'] = deepcopy(getattr(model, 'ledger', [])[transport_start:])
            audit['first_report_sha256'] = first_hash
            audit['evidence_registry'] = registry.entries
            audit['label'] = (
                '补查后更正' if audit['stop_reason'] == 'candidate_accepted_under_model_review' and audit['changed']
                else '补查后维持' if audit['stop_reason'] == 'candidate_accepted_under_model_review' or (audit['stop_reason'] == 'no_doubt_found' and (row.get('validation') or {}).get('valid'))
                else '仍有疑点' if audit['stop_reason'] == 'semantic_not_supported_or_unknown'
                else '复核未完成')
            # No-doubt discovery did not perform a supplementary review.
            audit['intervened'] = audit['first_questioned'] or audit['stop_reason'] != 'no_doubt_found'
            write_new(folder / f'{task.task_id}-selection.json', audit)
            selected['agent_reviews'].append(dict(task_id=task.task_id, **audit))
            target = next(r for r in selected['results'] if r['task_id'] == task.task_id)
            target['agent'] = audit
            if audit['stop_reason'] == 'candidate_accepted_under_model_review':
                target['validation'] = business.checked
                target['business_conclusion'] = business.checked['reply']['conclusion']
            if audit['intervened']:
                target['manual_review_required'] = True
            selected['budget'] = pool.snapshot()
            write_new(folder / f'selected-progress-{len(selected["agent_reviews"])}.json', selected)
    selected['budget'] = pool.snapshot()
    write_new(folder / 'selected-report.json', selected)
    return selected
