"""Independent automatic chain. Model opinions are not human adjudication."""
import json
from dataclasses import asdict, dataclass, field
from time import monotonic
from typing import Callable, Protocol, TypedDict

from langgraph.graph import END, START, StateGraph

from .engine import JUDGMENTS, SYSTEM, TRIGGERS, Evidence, Reply, Result, ReviewInput


class SharedPool(Protocol):
    """Atomically hold capacity in caller's TOTAL budget, including prior stages.

    finish releases unused holds and charges actual requests/tokens. Must support
    concurrent callers without independent quotas. No model calls inside methods.
    """
    def hold(self, requests: int, tokens: int) -> bool: ...
    def finish(self, held_requests: int, held_tokens: int,
               actual_requests: int, actual_tokens: int) -> None: ...


@dataclass(frozen=True)
class Signals:
    # Explicit assertions/mappings from upstream; absence alone proves nothing.
    explicitly_unapproved: bool = False
    material_retrievable: bool = False
    policy_dependencies: tuple[str, ...] = ()
    policy_evidence_map: dict[str, tuple[str, ...]] = field(default_factory=dict)
    unsupported_claims: tuple[str, ...] = ()


@dataclass
class AutoPorts:
    model: Callable[[dict], Reply]  # raw JSON TEXT inside Reply, never Result objects
    search: Callable[[dict], Reply]
    locate: Callable[[Result, ReviewInput, tuple[Evidence, ...]], bool]
    save: Callable[[dict], None]
    pool: SharedPool
    model_tokens: int = 1024
    search_tokens: int = 0
    seconds: float = 30
    clock: Callable[[], float] = monotonic
    business: object | None = None  # Compatible full-candidate protocol; same graph and gates.
    model_caps: dict | None = None
    checkpoint: Callable[[dict], None] | None = None


class AutoState(TypedDict):
    report: dict


FIELDS = ('contract_fact', 'policy_requirement', 'comparison', 'approval_status', 'reason')


def strict_json(raw):
    def pairs(items):
        obj = {}
        for key, value in items:
            if key in obj:
                raise ValueError('duplicate JSON key')
            obj[key] = value
        return obj
    if not isinstance(raw, str):
        raise ValueError('model must return raw JSON text')
    return json.loads(raw, object_pairs_hook=pairs,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError('nonfinite JSON')))


def keys(obj, expected):
    if not isinstance(obj, dict) or set(obj) != set(expected):
        raise ValueError('incorrect response fields')


def nonempty(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError('empty text')
    return value


def evidence_ids(ids, evidence):
    if not isinstance(ids, list) or any(not isinstance(i, str) or i not in evidence for i in ids):
        raise ValueError('unknown evidence ID')
    return tuple(evidence[i] for i in ids)


def parse_doubts(raw, evidence, item):
    obj = strict_json(raw)
    keys(obj, ('status', 'questions', 'limitations'))
    if obj['status'] not in ('completed', 'unknown', 'failed'):
        raise ValueError('invalid discovery status')
    if not isinstance(obj['questions'], list) or not isinstance(obj['limitations'], list):
        raise ValueError('invalid discovery lists')
    for value in obj['limitations']:
        nonempty(value)
    for q in obj['questions']:
        keys(q, ('trigger', 'question', 'basis', 'evidence_ids', 'source', 'snapshot', 'resolvable'))
        if q['trigger'] not in TRIGGERS or q['source'] not in ('contract', 'policy'):
            raise ValueError('invalid question scope')
        if q['snapshot'] != getattr(item, q['source'] + '_snapshot'):
            raise ValueError('question snapshot mismatch')
        if type(q['resolvable']) is not bool:
            raise ValueError('invalid resolvability')
        nonempty(q['question'])
        nonempty(q['basis'])
        evidence_ids(q['evidence_ids'], evidence)
        if not q['evidence_ids']:
            raise ValueError('model doubt requires observed evidence')
    return obj


def parse_candidate(raw, evidence):
    obj = strict_json(raw)
    keys(obj, ('judgment', *FIELDS, 'evidence_ids'))
    if obj['judgment'] not in JUDGMENTS:
        raise ValueError('invalid judgment')
    for name in FIELDS:
        nonempty(obj[name])
    return Result(judgment=obj['judgment'], **{name: obj[name] for name in FIELDS},
                  citations=evidence_ids(obj['evidence_ids'], evidence))


def parse_semantics(raw, candidate, evidence):
    obj = strict_json(raw)
    keys(obj, ('status', 'dimensions', 'model_coverage_claim'))
    if obj['status'] not in ('completed', 'unknown', 'failed'):
        raise ValueError('invalid semantic status')
    if type(obj['model_coverage_claim']) is not bool:
        raise ValueError('invalid coverage claim')
    if not isinstance(obj['dimensions'], list) or len(obj['dimensions']) != len(FIELDS):
        raise ValueError('all five dimensions required')
    seen = set()
    for dim in obj['dimensions']:
        keys(dim, ('field', 'text', 'claims', 'conditions', 'exceptions', 'counterevidence'))
        name = dim['field']
        if name not in FIELDS or name in seen or dim['text'] != getattr(candidate, name):
            raise ValueError('dimension missing, duplicated or text altered')
        seen.add(name)
        for name in ('conditions', 'exceptions', 'counterevidence'):
            nonempty(dim[name])
        if not isinstance(dim['claims'], list) or not dim['claims']:
            raise ValueError('empty assertion assessment')
        for claim in dim['claims']:
            keys(claim, ('assertion', 'status', 'evidence_ids', 'explanation'))
            nonempty(claim['assertion'])
            nonempty(claim['explanation'])
            if claim['status'] not in ('supported', 'unsupported', 'unknown'):
                raise ValueError('invalid claim support')
            evidence_ids(claim['evidence_ids'], evidence)
            if claim['status'] == 'supported' and not claim['evidence_ids']:
                raise ValueError('supported claim without evidence')
    # Program proves schema/dimension presence ONLY. Splitting and coverage remain model opinions.
    return obj


def deterministic(item, signals):
    questions = []
    def add(trigger, question, basis, source, resolvable=True):
        questions.append(dict(trigger=trigger, question=question, basis=basis,
                              evidence_ids=[], source=source,
                              snapshot=getattr(item, source + '_snapshot'), resolvable=resolvable))
    if signals.explicitly_unapproved and item.first.approval_status == 'unknown':
        add('unknown_material_as_violation', 'Can fixed materials establish approval status?',
            'Explicit unapproved assertion contradicts structured unknown approval.',
            'contract', signals.material_retrievable)
    for condition in signals.policy_dependencies:
        if condition in signals.policy_evidence_map and not signals.policy_evidence_map[condition]:
            add('missing_policy_condition', 'Find applicable condition: ' + condition,
                'Explicit dependency has an explicitly empty evidence mapping.', 'policy')
    for claim in signals.unsupported_claims:
        add('unsupported_assertion', 'Verify assertion: ' + claim,
            'Upstream explicitly recorded unsupported assertion: ' + claim, 'contract')
    return questions


def automatic_review(item: ReviewInput, ports: AutoPorts, signals: Signals = Signals()):
    start = ports.clock()
    r = dict(first=asdict(item.first), first_validation=asdict(item.first_validation),
             effective=None, candidate=None, discovery=None, semantic_review=None,
             first_questioned=False, first_status='not_admitted', human_adjudication='not_performed',
             ledger=[], checked_questions=[], uncovered_questions=[], limitations=[],
             stop_reason='', errors=[], saved=False, changed=False, field_changes={},
             program_checks={}, usage={'requests': 0, 'tokens': 0, 'elapsed_seconds': 0})
    ev = {f'e{i}': e for i, e in enumerate(item.evidence)}
    held = [0, 0]
    candidate = [None]
    business = ports.business
    if business:
        r['first'] = business.first
        r['first_validation'] = business.first_validation

    def call(stage, fn, payload, cap):
        if ports.clock() - start >= ports.seconds:
            raise TimeoutError('deadline_before_call')
        event = dict(stage=stage, request=payload, status='started', tokens=cap, usage_known=False)
        r['ledger'].append(event)
        r['usage']['requests'] += 1
        begun = ports.clock()
        try:
            reply = fn(payload)
            if not isinstance(reply, Reply) or type(reply.tokens) is not int or reply.tokens < 0:
                raise ValueError('invalid usage envelope')
            logged = ([asdict(e) if isinstance(e, Evidence) else e for e in reply.value]
                      if isinstance(reply.value, (list, tuple)) else reply.value)
            event.update(response=logged, tokens=reply.tokens, input_tokens=reply.input_tokens,
                         status='returned', usage_known=True)
            if reply.tokens > cap:
                raise ValueError('token cap exceeded')
            if ports.clock() - start >= ports.seconds:
                raise TimeoutError('deadline_after_call')
            return reply.value
        except Exception as exc:
            event.update(status='failed', error=str(exc))
            if getattr(exc, 'usage_known', False):
                event.update(tokens=exc.output_tokens, usage_known=True)
            raise
        finally:
            event['elapsed_seconds'] = ports.clock() - begun
            r['usage']['tokens'] += event['tokens']

    def model(stage, instruction, extra):
        if business:
            instruction = business.instruction(stage, instruction)
        cap = (ports.model_caps or {}).get(stage, ports.model_tokens)
        return call(stage, ports.model,
                    dict(stage=stage, system=SYSTEM + instruction,
                         check_id=item.check_id, criterion=item.criterion,
                         snapshots={'contract': item.contract_snapshot, 'policy': item.policy_snapshot},
                         evidence={k: asdict(v) for k, v in ev.items()},
                         data=business.payload(stage, extra) if business else extra,
                         max_tokens=cap), cap)

    def guard(fn):
        def wrapped(state):
            try:
                fn()
            except Exception as exc:
                r['errors'].append(str(exc))
                r['stop_reason'] = fn.__name__ + '_failed'
                if fn.__name__ == 'discover':
                    r['discovery'] = dict(status='failed', questions=[], error=str(exc))
                if fn.__name__ == 'search':
                    for entry in r['uncovered_questions']:
                        entry['reason'] = 'search_failed_or_not_started'
                if fn.__name__ == 'semantic':
                    r['semantic_review'] = dict(status='failed', error=str(exc))
            if ports.checkpoint:
                try:
                    ports.checkpoint(r.copy())
                except Exception:
                    r['errors'].append('checkpoint_save_failed')
                    r['stop_reason'] = 'checkpoint_save_failed'
            return {'report': r}
        return wrapped

    def admit():
        for name in ('contract_snapshot', 'policy_snapshot', 'check_id', 'criterion'):
            nonempty(getattr(item, name))
        if ports.model_tokens < 1 or ports.search_tokens < 0 or ports.seconds <= 0:
            raise ValueError('invalid execution limits')
        for e in (*item.evidence, *item.first.citations):
            if e.source not in ('contract', 'policy') or e.snapshot != getattr(item, e.source + '_snapshot'):
                raise ValueError('invalid source binding')
            nonempty(e.location)
            nonempty(e.quote)
        if any(e not in item.evidence for e in item.first.citations):
            raise ValueError('first citation outside initial evidence')
        if item.questions:
            raise ValueError('automatic entry rejects prefilled questions')
        r['program_checks']['input_binding'] = True
        if (business.first_validation.get('valid') if business else
                item.first_validation.valid and item.first.judgment in JUDGMENTS):
            r['effective'] = business.first if business else asdict(item.first)
            r['first_status'] = 'retained_under_original_validation'

    def discover():
        qs = deterministic(item, signals)
        if qs:
            r['first_questioned'] = True
            r['first_status'] = 'questioned_original_record_retained'
        if qs and not any(q['resolvable'] for q in qs):
            r['discovery'] = dict(status='completed', questions=qs, limitations=[],
                                  method='deterministic')
            r['limitations'].extend(qs)
            r['first_questioned'] = True
            r['first_status'] = 'questioned_original_record_retained'
            r['stop_reason'] = 'limitations_only'
            return
        # Reserve ALL potential downstream calls atomically before any model call.
        count = 4 if qs else 5
        stages = ('generation', 'semantic') if qs else ('discovery', 'generation', 'semantic')
        tokens = sum((ports.model_caps or {}).get(s, ports.model_tokens) for s in stages) + 2 * ports.search_tokens
        if not ports.pool.hold(count, tokens):
            r['stop_reason'] = 'shared_budget_insufficient_for_complete_chain'
            r['discovery'] = dict(status='unknown', questions=qs)
            return
        held[:] = [count, tokens]
        if qs:
            output = dict(status='completed', questions=qs, limitations=[], method='deterministic')
        else:
            raw = model('discovery', DISCOVERY_SCHEMA, {'first': r['first'], 'first_validation': r['first_validation']})
            output = parse_doubts(raw, ev, item)
            output['method'] = 'model'
        r['discovery'] = output
        r['limitations'].extend(output['limitations'])
        if output['questions']:
            r['first_questioned'] = True
            r['first_status'] = 'questioned_original_record_retained'
        if output['status'] != 'completed':
            r['stop_reason'] = 'discovery_' + output['status']
            return
        searchable = []
        for q in output['questions']:
            if q['resolvable']:
                searchable.append(q)
            else:
                r['limitations'].append(q)
        r['uncovered_questions'] = [{'index': i, 'question': q, 'reason': 'not_searched'}
                                     for i, q in enumerate(searchable)]
        if not searchable:
            r['stop_reason'] = 'limitations_only' if output['questions'] else 'no_doubt_found'

    def search():
        for entry in r['uncovered_questions'][:2]:
            q = entry['question']
            response = call('search', ports.search, q | {'max_tokens': ports.search_tokens},
                            ports.search_tokens)
            if not isinstance(response, (tuple, list)):
                raise ValueError('search must return evidence list')
            for e in response:
                if not isinstance(e, Evidence) or e.source != q['source'] or e.snapshot != q['snapshot']:
                    raise ValueError('search scope violation')
                nonempty(e.location)
                nonempty(e.quote)
            for e in response:
                ev['e' + str(len(ev))] = e
            entry['reason'] = 'completed'
            r['uncovered_questions'] = [x for x in r['uncovered_questions']
                                        if x['index'] != entry['index']]
            r['checked_questions'].append(entry | {'evidence': [asdict(e) for e in response]})
        r['uncovered_questions'] = [entry | {'reason': 'search_limit'}
                                     for entry in r['uncovered_questions']]

    def generate():
        raw = model('generation', CANDIDATE_SCHEMA,
                    {'first': r['first'], 'first_validation': r['first_validation'], 'checked': r['checked_questions'],
                     'uncovered': r['uncovered_questions']})
        candidate[0] = business.parse(raw) if business else parse_candidate(raw, ev)
        r['candidate'] = candidate[0] if business else asdict(candidate[0])
        r['program_checks']['candidate_structure_and_binding'] = True

    def locate():
        if ports.locate(candidate[0], item, tuple(ev.values())) is not True:
            r['stop_reason'] = 'candidate_location_rejected'
            r['program_checks']['candidate_location'] = False
        else:
            r['program_checks']['candidate_location'] = True

    def semantic():
        raw = model('semantic', SEMANTIC_SCHEMA, {'candidate': r['candidate'],
                    'uncovered': r['uncovered_questions']})
        output = business.semantics(raw, candidate[0]) if business else parse_semantics(raw, candidate[0], ev)
        r['semantic_review'] = output | {'object': 'candidate', 'authority': 'model_opinion'}
        accepted = (output['status'] == 'completed' and output['model_coverage_claim'] and
                    all(c['status'] == 'supported' for d in output['dimensions'] for c in d['claims']))
        if accepted:
            r['effective'] = r['candidate']
            r['field_changes'] = (business.changes(candidate[0]) if business else
                                 {name: getattr(candidate[0], name) != getattr(item.first, name)
                                  for name in ('judgment', *FIELDS, 'citations')})
            r['changed'] = any(r['field_changes'].values()) if business else r['field_changes']['judgment']
            r['stop_reason'] = 'candidate_accepted_under_model_review'
        else:
            r['stop_reason'] = 'semantic_not_supported_or_unknown'

    def save(state):
        r['usage']['elapsed_seconds'] = ports.clock() - start
        if held[0]:
            try:
                if hasattr(ports.pool, 'finish_review'):
                    ports.pool.finish_review(*held, r['ledger'])
                else:
                    ports.pool.finish(*held, r['usage']['requests'], r['usage']['tokens'])
            except Exception as exc:
                r['errors'].append('budget_finish_failed: ' + str(exc))
        try:
            r['saved'] = True
            ports.save(r.copy())
        except Exception as exc:
            r['saved'] = False
            r['errors'].append('save_failed: ' + str(exc))
        return {'report': r}

    graph = StateGraph(AutoState)
    names = [('admit', admit), ('discover', discover), ('search', search),
             ('generate', generate), ('locate', locate), ('semantic', semantic)]
    for name, fn in names:
        graph.add_node(name, guard(fn))
    graph.add_node('save', save)
    graph.add_edge(START, 'admit')
    for i, (name, _) in enumerate(names):
        target = names[i + 1][0] if i + 1 < len(names) else 'save'
        graph.add_conditional_edges(name, lambda state, target=target:
                                    'save' if r['stop_reason'] else target)
    graph.add_edge('save', END)
    return graph.compile().invoke({'report': r})['report']


DISCOVERY_SCHEMA = ''' Analyze possible conflicts, omitted exceptions and unsupported reasoning.
These are suspicions, never established facts. Return ONLY JSON with status
(completed|unknown|failed), limitations (string list), questions (list of exact objects:
trigger (missing_policy_condition|clause_conflict|unsupported_assertion|unknown_material_as_violation),
question, basis, evidence_ids (nonempty allowed IDs), source (contract|policy), snapshot,
resolvable (boolean)). Missing uploads/external facts are not retrievable. No confidence routing.'''
CANDIDATE_SCHEMA = ''' Return ONLY JSON with judgment (compliant|deviation|uncertain|withdrawn),
contract_fact, policy_requirement, comparison, approval_status, reason (all nonempty strings,
use unknown when unknown), evidence_ids (list of allowed IDs). Consider unresolved doubts.'''
SEMANTIC_SCHEMA = ''' Independently split ALL material assertions in each full candidate dimension,
including judgment implications in comparison/reason. Do not accept candidate-selected claims as coverage.
Assess conditions, exceptions and counterevidence for every dimension. Return ONLY JSON:
status (completed|unknown|failed), model_coverage_claim (boolean; opinion only), dimensions
(exactly five entries for contract_fact, policy_requirement, comparison, approval_status, reason).
Each entry has field, text (exact full candidate field), conditions, exceptions, counterevidence
(nonempty explanations), claims (nonempty list). Each claim has assertion, status
(supported|unsupported|unknown), evidence_ids (allowed IDs), explanation. Unknown evidence means unknown,
not supported. No human adjudication is performed by this request.'''
