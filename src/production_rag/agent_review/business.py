"""Full v3 candidate protocol plugged into the existing automatic graph."""
import json
from copy import deepcopy

from production_rag.pilot_review import context
from production_rag.pilot_review import findings_v2 as v3
from production_rag.pilot_review.validation import Citation, locate
from production_rag.review_inputs.models import digest

from .automatic import keys, nonempty, strict_json


def admit_first(run, prepared, row):
    """Admission is source safety only, never business acceptance or field repair."""
    try:
        if row.get('failure') in ('truncated', 'empty_content', 'usage_unknown', 'transport'):
            raise ValueError('unsafe_transport_result')
        obj = strict_json(row.get('final_answer'))
        if not isinstance(obj, dict) or not obj:
            raise ValueError('empty_or_nonobject_reply')
        if prepared['maps'] != context.source_maps(run, prepared['retrieval']):
            raise ValueError('source_map_mismatch')
        registry = prepared['evidence_registry']
        for entry in registry.values():
            found = locate(run, prepared['maps'], Citation(**{k: entry[k] for k in Citation.model_fields}))
            if (found['start'], found['end']) != (entry['start'], entry['end']):
                raise ValueError('source_map_mismatch')
        def check(value):
            if isinstance(value, dict):
                for key, child in value.items():
                    if key.endswith('evidence_ids'):
                        if not isinstance(child, list) or any(not isinstance(e, str) or e not in registry for e in child):
                            raise ValueError('unknown_evidence_id')
                    elif key in ('document_id', 'block_id', 'source_type', 'quote'):
                        raise ValueError('unregistered_inline_source')
                    else:
                        check(child)
            elif isinstance(value, list):
                for child in value:
                    check(child)
        check(obj)
        return dict(admitted=True, reason='validated_first' if (row.get('validation') or {}).get('valid') else 'unverified_business_material_only')
    except (ValueError, TypeError, KeyError):
        return dict(admitted=False, reason='first_source_or_json_not_admitted')


def dimensions(candidate):
    result = {}
    for part in candidate['comparisons']:
        for field in ('conclusion', 'fact', 'requirement', 'reason', 'suggestion', 'pending',
                      'fact_scope', 'limitation'):
            result[part['part_id'] + '.' + field] = part[field]
    for field in ('conclusion', 'approval_status', 'approval_trigger', 'requested_material_ids'):
        result[field] = candidate[field]
    return result


def scoped_dimensions(candidate):
    return {**{p['part_id']: {k: v for k, v in p.items() if k != 'part_id'}
               for p in candidate['comparisons']},
            'approval': {k: candidate[k] for k in ('approval_status', 'approval_trigger',
                                                  'approval_evidence_ids', 'requested_material_ids')},
            'aggregate': {'conclusion': candidate['conclusion']}}


def candidate_digest(candidate):
    return digest(json.dumps(candidate, ensure_ascii=False, sort_keys=True).encode())


class BusinessCandidate:
    def __init__(self, run, task, prepared, row, semantic_protocol='fields-v1'):
        self.run, self.task = run, task
        self.prepared = deepcopy(prepared)
        self.first = strict_json(row['final_answer'])
        self.first_validation = deepcopy(row.get('validation') or dict(valid=False, failure=row.get('failure')))
        self.checked = None
        self.semantic_protocol = semantic_protocol

    def instruction(self, stage, fallback):
        if stage == 'discovery' and self.semantic_protocol == 'scoped-v1':
            return fallback + ' Discovery evidence_ids MUST use top-level evidence keys e0,e1,...; business registry IDs are only for v3 generation/semantic. At most two specific questions.'
        if stage == 'generation':
            return v3.PROMPT_V3 + ' Return ONLY the complete CompactReply v3 object specified by candidate_schema. Preserve all part_id values and independent approval.'
        if stage == 'semantic':
            if self.semantic_protocol == 'scoped-v1':
                return (' Independently assess the ACTUAL COMPLETE v3 candidate, every subcomparison and approval. '
                        'Return ONLY JSON {status:completed|unknown|failed,model_coverage_claim:boolean,'
                        'candidate_sha256:the supplied digest,dimensions:[{field:scope id,'
                        'checks:{every supplied scope field:supported|unsupported|unknown},evidence_ids:[allowed IDs],'
                        'explanation:brief evidence support,conditions:brief,exceptions:brief,counterevidence:brief}]}. '
                        'Use semantic_scopes for exact scopes and fields. Do not repeat the original text; the whole '
                        'candidate is provided. Assess all facts, requirements, conclusions, reasons, suggestions, '
                        'limitations and approval independently. Unknown facts stay unknown. Findings are model opinion '
                        'only, never human adjudication. Supported requires cited evidence.')
            return (' Independently review the ACTUAL COMPLETE v3 candidate. Return ONLY JSON with '
                    'status (completed|unknown|failed), model_coverage_claim (boolean), dimensions. '
                    'For EVERY comparison include part_id.field dimensions for conclusion, fact, requirement, reason, '
                    'suggestion, pending, fact_scope, limitation. Also include top-level conclusion, approval_status, '
                    'approval_trigger, requested_material_ids. Each dimension: field, text (exact full original value), '
                    'conditions, exceptions, counterevidence (nonempty explanations), claims (nonempty list with '
                    'assertion,status supported|unsupported|unknown,evidence_ids,explanation). '
                    'Use evidence IDs from business_request.evidence_registry. Do not infer unknown facts. '
                    'This is model opinion, never human adjudication. Review all conditions and counterevidence.')
        return fallback

    def payload(self, stage, extra):
        if self.semantic_protocol == 'scoped-v1':
            # Exclude the redundant nested prepared.messages/maps/raw encodings. Exact evidence
            # spans, full facts, formal validation, parts and definitions remain explicit.
            request = json.loads(self.prepared['messages'][1]['content'])
            data = dict({k: v for k, v in extra.items() if k not in ('first', 'first_validation')},
                        business_first=self.first, first_formal_validation={
                            k: self.first_validation.get(k) for k in ('valid', 'failure', 'semantic_support', 'business_definition_version')},
                        business_definition=request.get('business_definition'), parts=self.prepared['parts'],
                        evidence_registry=self.prepared['evidence_registry'])
            if stage == 'generation':
                data['candidate_schema'] = v3.CompactReply.model_json_schema()
            if stage == 'semantic':
                data['candidate_sha256'] = candidate_digest(extra['candidate'])
                data['semantic_scopes'] = {k: list(v) for k, v in scoped_dimensions(extra['candidate']).items()}
            return data
        return dict(extra, business_first=self.first,
                    business_first_validation=self.first_validation,
                    business_request=self.prepared,
                    candidate_schema=v3.CompactReply.model_json_schema(),
                    semantic_dimensions='Every part: conclusion,fact,requirement,reason,suggestion,pending,fact_scope,limitation; '
                    'plus conclusion,approval_status,approval_trigger,requested_material_ids. '
                    'Return dimensions [{field,text,conditions,exceptions,counterevidence,claims:[{assertion,status,evidence_ids,explanation}]}], '
                    'status and model_coverage_claim. text must exactly equal actual field value. '
                    'Use business evidence_registry IDs. Model opinion only. '
                    'Generation returns ONLY complete v3 JSON, never flat Result.')

    def parse(self, raw):
        parsed = strict_json(raw)
        # Full original raw candidate goes through exact product locator and aggregation.
        self.checked = v3.validate_v3(self.run, self.task, self.prepared, raw)
        if not self.checked['valid']:
            raise ValueError('v3_candidate_rejected')
        return parsed

    def locate(self, candidate, item, evidence):
        return self.checked is not None and self.checked['valid'] is True

    def changes(self, candidate):
        try:
            before = dimensions(self.first)
        except (KeyError, TypeError):
            before = {}
        return {key: before.get(key) != value for key, value in dimensions(candidate).items()}

    def semantics(self, raw, candidate):
        if self.semantic_protocol == 'scoped-v1':
            return self.scoped_semantics(raw, candidate)
        obj = strict_json(raw)
        keys(obj, ('status', 'dimensions', 'model_coverage_claim'))
        if obj['status'] not in ('completed', 'unknown', 'failed') or type(obj['model_coverage_claim']) is not bool:
            raise ValueError('invalid_semantic_status')
        expected = dimensions(candidate)
        seen = set()
        for dim in obj['dimensions']:
            keys(dim, ('field', 'text', 'conditions', 'exceptions', 'counterevidence', 'claims'))
            field = dim['field']
            if field in seen or field not in expected or dim['text'] != expected[field]:
                raise ValueError('semantic_full_candidate_mismatch')
            seen.add(field)
            for name in ('conditions', 'exceptions', 'counterevidence'):
                nonempty(dim[name])
            if not isinstance(dim['claims'], list) or not dim['claims']:
                raise ValueError('empty_semantic_claims')
            for claim in dim['claims']:
                keys(claim, ('assertion', 'status', 'evidence_ids', 'explanation'))
                nonempty(claim['assertion'])
                nonempty(claim['explanation'])
                if claim['status'] not in ('supported', 'unsupported', 'unknown'):
                    raise ValueError('invalid_support')
                ids = claim['evidence_ids']
                if not isinstance(ids, list) or any(i not in self.prepared['evidence_registry'] for i in ids):
                    raise ValueError('semantic_unknown_evidence')
                if claim['status'] == 'supported' and not ids:
                    raise ValueError('semantic_support_without_evidence')
        if seen != set(expected):
            raise ValueError('semantic_missing_part_or_approval')
        return obj

    def scoped_semantics(self, raw, candidate):
        obj = strict_json(raw)
        keys(obj, ('status', 'model_coverage_claim', 'candidate_sha256', 'dimensions'))
        if obj['candidate_sha256'] != candidate_digest(candidate):
            raise ValueError('semantic_candidate_digest_mismatch')
        if obj['status'] not in ('completed', 'unknown', 'failed') or type(obj['model_coverage_claim']) is not bool:
            raise ValueError('invalid_semantic_status')
        scopes = scoped_dimensions(candidate)
        seen = set()
        normalized = []
        for dim in obj['dimensions']:
            keys(dim, ('field', 'checks', 'evidence_ids', 'explanation', 'conditions', 'exceptions', 'counterevidence'))
            name = dim['field']
            if name in seen or name not in scopes:
                raise ValueError('semantic_scope_mismatch')
            seen.add(name)
            keys(dim['checks'], scopes[name])
            ids = dim['evidence_ids']
            if not isinstance(ids, list) or any(i not in self.prepared['evidence_registry'] for i in ids):
                raise ValueError('semantic_unknown_evidence')
            for field in ('explanation', 'conditions', 'exceptions', 'counterevidence'):
                nonempty(dim[field])
            claims = []
            for field, status in dim['checks'].items():
                if status not in ('supported', 'unsupported', 'unknown') or (status == 'supported' and not ids):
                    raise ValueError('invalid_semantic_support')
                claims.append(dict(assertion=field, status=status, evidence_ids=ids, explanation=dim['explanation']))
            normalized.append(dict(field=name, text=scopes[name], claims=claims,
                conditions=dim['conditions'], exceptions=dim['exceptions'], counterevidence=dim['counterevidence']))
        if seen != set(scopes):
            raise ValueError('semantic_missing_part_or_approval')
        return dict(status=obj['status'], model_coverage_claim=obj['model_coverage_claim'],
                    dimensions=normalized, raw_scoped_review=obj, protocol='scoped-v1')
