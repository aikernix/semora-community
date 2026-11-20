"""Query repairs preserve selected tables, publish only proof, and retain gaps."""
import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from semora_contracts import QueryRun, QueryUnderstanding, QueryConfigurationGap, OntologySnapshotRef
from semora_agent.model import TaskSupersededError
from app import prepared_context
from app.query_relation_repair import supplement, gap_pairs
from app.relationship_keywords import KeywordDiscovery
from app.relationship_normalization import join_signature


@pytest.fixture
def setup(monkeypatch, tmp_path):
    monkeypatch.setenv('RELATIONSHIP_CHECKPOINT_DIR', str(tmp_path))
    tables = [dict(id=name, name=name, kind='TABLE', columns=[dict(name=field, type='INT')],
                   primary_keys=[dict(columns=[field])], unique_constraints=[])
              for name, field in [('roots', 'key'), ('details', 'root_key'), ('unselected', 'key')]]
    objects = [dict(name=t['name'], table=t['id'], dataSourceId='a', metadata={'sourceTableId': t['id']},
                    properties=[dict(name=c['name'], column=c['name'], type='integer') for c in t['columns']]) for t in tables]
    ontology = dict(domain='test', dataSourceId='a', objects=objects, relations=[], metrics=[], knowledgeBaseIds=[])
    stored_package = dict(schema_hash='schema', payload=deepcopy(ontology))
    source = SimpleNamespace(id='a', type='postgresql', scan_result={'tables': tables}, status='healthy', last_scan_at=None)
    source_repository = SimpleNamespace(get=lambda _: source)
    monkeypatch.setattr(prepared_context.datasource_service, 'repository', source_repository)
    repository = SimpleNamespace(memory_packages={}, get_package=lambda _: deepcopy(stored_package), list_candidates=lambda _: [])
    service = SimpleNamespace(datasources=SimpleNamespace(repository=source_repository), repository=repository,
                              source_locks={}, _profile=AsyncMock(return_value={'matched_rows': 4, 'sampled_rows': 4}))
    async def review(candidates, maps, job, **kwargs):
        assert kwargs == {'discover': False}
        assert set(maps['a']) == {'roots', 'details'}
        for candidate in candidates:
            assert any(e['kind'] == 'value_profile' for e in candidate['evidence'])
            candidate.update(status='accepted', cardinality='N:1')
    service._semantic_review = AsyncMock(side_effect=review)
    async def publish(value):
        assert value['appendRelations'] is True
        assert set(value['expectedTableHashes']) == {'roots', 'details', 'unselected'}
        assert 'objects' not in value['payload']  # not a replacement of the package
        stored_package['payload']['relations'].extend(value['payload']['relations'])
    service.ontology = SimpleNamespace(update_source_package=AsyncMock(side_effect=publish), append_cross_relations=AsyncMock())
    run = QueryRun(session_id='s', domain='test', question='Show details with entity information')
    run.ontology = OntologySnapshotRef(domain='test', version='old', content_hash='old', artifact_version='1')
    async def snapshot(_):
        return run.ontology.model_copy(update={'version': 'new', 'content_hash': 'new'}), deepcopy(stored_package['payload'])
    workflow = SimpleNamespace(emit=lambda *args: None, _ontology_snapshot=AsyncMock(side_effect=snapshot))
    saved = {'packet': {'selected_objects': ['roots', 'details'], 'knowledge': ['retained']}, 'binding': 'old'}
    report = QueryUnderstanding(summary='Show details', target_state='known', required_object_uses=[
        dict(object=name, requirement='Show records with entity information', reason='Required output source')
        for name in ['roots', 'details']], technical_gaps=[QueryConfigurationGap(
        kind='relation', summary='Missing mapping', required_for='join', objects=['roots', 'details'],
        field_refs=['roots.key', 'details.root_key'], evidence_refs=['request'], resolution='Verify the mapping')])
    async def request(engine, kind, system, payload, contract):
        assert len(payload['tables']) == 2
        assert {t['name'] for t in payload['tables']} == {'roots', 'details'}
        ids = {t['name']: t['table_id'] for t in payload['tables']}
        return {'relations': [dict(source_table_id=ids['details'], target_table_id=ids['roots'],
                                   key_pairs=[dict(source='root_key', target='key')], reason='References entity key')]}
    monkeypatch.setattr(KeywordDiscovery, 'request', request)
    return SimpleNamespace(service=service, run=run, report=report, ontology=ontology, saved=saved,
                           workflow=workflow, source=source, package=stored_package)


def execute(s):
    return asyncio.run(supplement(s.workflow, s.run, s.run.question, s.ontology, {}, s.saved, s.report, service=s.service))


def test_validated_relation_publishes_refreshes_only_selected_tables(setup):
    s = setup
    s.report.summary = 'The relation is not configured and the query cannot run'
    s.report.skeleton = ['Wait for the missing configuration']
    s.report.requirements.sequence = ['Read the requested records', 'Include the corresponding information']
    assert execute(s)
    assert s.report.technical_gaps == []
    assert s.run.ontology.version == 'new'
    assert s.report.summary == s.run.question
    assert s.report.skeleton == s.report.requirements.sequence
    assert s.saved['packet']['selected_objects'] == ['details', 'roots']
    assert s.saved['packet']['knowledge'] == ['retained']
    assert len(s.ontology['objects']) == 3  # full ontology retained; packet stays bounded
    assert len(s.saved['packet']['relations']) == 1
    assert 'confidence' not in str(s.service.ontology.update_source_package.await_args.kwargs)
    candidate = s.service._semantic_review.await_args.args[0][0]
    assert candidate['domain'] is None  # single-source artifact is reusable by all bound domains
    assert s.service._profile.await_count == 1


@pytest.mark.parametrize('status', ['unresolved', 'rejected', 'accepted'])
def test_unproven_or_unknown_cardinality_never_writes(setup, status):
    async def review(candidates, *args, **kwargs):
        candidates[0].update(status=status, cardinality='unknown' if status == 'accepted' else 'N:1')
    setup.service._semantic_review.side_effect = review
    assert not execute(setup)
    assert setup.report.technical_gaps
    assert '技术人员' in setup.report.technical_gaps[0].resolution
    setup.service.ontology.update_source_package.assert_not_awaited()
    assert setup.run.ontology.version == 'old'


def test_tool_failure_remains_gap_and_same_attempt_is_not_repeated(setup):
    setup.service._profile.side_effect = TimeoutError('No delivery')
    assert not execute(setup)
    assert not execute(setup)
    assert setup.service._profile.await_count == 1
    setup.service._semantic_review.assert_not_awaited()
    setup.service.ontology.update_source_package.assert_not_awaited()


def test_cancel_or_requirement_change_prevents_publication(setup):
    async def profile(*args):
        setup.run.agent_memory['user_cancelled'] = [{'requested': True}]
        return {}
    setup.service._profile.side_effect = profile
    with pytest.raises(TaskSupersededError): execute(setup)
    setup.service.ontology.update_source_package.assert_not_awaited()


def test_schema_change_during_evidence_preserves_previous_package(setup):
    async def review(candidates, *args, **kwargs):
        setup.source.scan_result['tables'][0]['columns'].append(dict(name='new_key', type='INT'))
        candidates[0]['status'] = 'accepted'
    setup.service._semantic_review.side_effect = review
    assert not execute(setup)
    setup.service.ontology.update_source_package.assert_not_awaited()
    assert setup.report.technical_gaps


def test_disabled_mapping_is_not_automatically_reenabled(setup):
    disabled = dict(source_datasource_id='a', target_datasource_id='a', source_table_id='details', target_table_id='roots',
                    key_pairs=[dict(source='root_key', target='key')], status='disabled', domain=None)
    setup.service.repository.list_candidates = lambda _: [disabled]
    assert not execute(setup)
    setup.service._profile.assert_not_awaited()


def test_conditional_or_outside_scope_gaps_do_not_expand_selection(setup):
    gap = setup.report.technical_gaps[0]
    gap.objects = ['roots', 'unselected']
    assert not execute(setup)
    setup.service._profile.assert_not_awaited()
    gap.objects = ['roots', 'details']
    gap = QueryConfigurationGap.model_validate({**gap.model_dump(), 'depends_on': [{'question_id': 'business', 'values': ['yes']}]})
    setup.report.technical_gaps = [gap]
    assert gap_pairs(setup.report, {'roots', 'details'}) == []


def test_invalid_proposal_does_not_discard_valid_sibling(setup, monkeypatch):
    original = KeywordDiscovery.request
    async def request(*args, **kwargs):
        value = await original(*args, **kwargs)
        value['relations'].insert(0, {**value['relations'][0], 'guards': [dict(side='source', field='missing', operator='eq', value=1)]})
        return value
    monkeypatch.setattr(KeywordDiscovery, 'request', request)
    assert execute(setup)
    assert setup.service._profile.await_count == 1
    assert setup.run.agent_memory['query_relation_repairs'][0]['issues'][0]['reason'] == 'invalid_proposal'


def test_cross_source_relation_uses_domain_append_only(setup):
    s = setup
    for catalog in (s.ontology, s.package['payload']):
        next(o for o in catalog['objects'] if o['name'] == 'details')['dataSourceId'] = 'b'
    second = SimpleNamespace(**{**s.source.__dict__, 'id': 'b'})
    s.service.datasources.repository.get = lambda sid: s.source if sid == 'a' else second
    async def review(candidates, maps, job, **kwargs):
        assert set(maps) == {'a', 'b'}
        assert set(maps['a']) == {'roots'} and set(maps['b']) == {'details'}
        candidates[0].update(status='accepted', cardinality='N:1')
    s.service._semantic_review.side_effect = review
    async def publish(value):
        assert value['domain'] == 'test'
        assert set(value['tableHashes']['a']) == {'roots'}
        assert set(value['tableHashes']['b']) == {'details'}
        s.package['payload']['relations'].extend(value['relations'])
    s.service.ontology.append_cross_relations.side_effect = publish
    assert execute(s)
    s.service.ontology.update_source_package.assert_not_awaited()
    assert s.service.ontology.append_cross_relations.await_count == 1
    assert not s.report.technical_gaps


def test_authoring_continues_with_refreshed_context_without_selecting_again(setup, monkeypatch):
    from app import sql_authoring, business_copy, database_routes
    from semora_contracts import SQLProgram
    s = setup
    monkeypatch.setattr(database_routes, 'modeling_service', s.service)
    s.saved['packet'].update(prepared_context.context_packet(s.workflow, s.run, s.ontology, ['roots', 'details']))
    async def prepare(*args):
        s.run.agent_memory['prepared_context'] = [s.saved]
        return s.saved
    select = AsyncMock(side_effect=prepare)
    monkeypatch.setattr(prepared_context, 'prepare_context', select)
    monkeypatch.setattr(prepared_context, 'confirmed_fact_context', lambda *args: [])
    monkeypatch.setattr(sql_authoring, 'context', lambda *args: {})
    monkeypatch.setattr(sql_authoring, 'run_agent', AsyncMock(return_value=(s.report.model_dump(mode='json'), [])))
    monkeypatch.setattr(business_copy, 'polish_public_text', AsyncMock())
    s.workflow.model = SimpleNamespace()
    def payload(workflow, run, question, draft):
        assert len(s.saved['packet']['relations']) == 1
        assert not draft.technical_gaps
        assert run.ontology.version == 'new'
        return {'request': {}}
    monkeypatch.setattr(sql_authoring, 'sql_context', payload)
    program = SQLProgram(summary='Show related records', steps=[dict(name='q', purpose='Show records',
        data_source_id='a', sql='SELECT d.root_key FROM details d JOIN roots r ON d.root_key = r.key', output_columns=['root_key'])])
    monkeypatch.setattr(sql_authoring, 'structured', AsyncMock(return_value=program))
    monkeypatch.setattr(sql_authoring, 'check_coverage', AsyncMock(return_value=program))
    outcome = asyncio.run(sql_authoring.analyse(s.workflow, s.run, s.run.question, s.ontology, {}))
    assert outcome.plan.sql_program.steps[0].sql == program.steps[0].sql
    assert select.await_count == 1


@pytest.mark.parametrize('proved', [True, False])
def test_technical_mapping_question_is_routed_to_proof_not_user_approval(setup, monkeypatch, proved):
    from semora_contracts import ClarificationQuestion, QuestionCondition
    from app import relationship_context
    setup.workflow.model = SimpleNamespace()
    setup.report.questions = [ClarificationQuestion(id='mapping', field='root_key', question='Which key joins these records?', reason='Name mapping'),
                              ClarificationQuestion(id='range', field='scope', question='Which records are in scope?', reason='Business scope')]
    setup.report.technical_gaps[0].depends_on = [QuestionCondition(question_id='mapping', values=['root_key'])]
    router = AsyncMock(return_value={'technical_question_ids': ['mapping', 'nonexistent']})
    monkeypatch.setattr(relationship_context, 'complete', router)
    if not proved:
        setup.service._profile.side_effect = TimeoutError()
    assert execute(setup) is proved
    assert [q.id for q in setup.report.questions] == ['range']
    if not proved:
        assert setup.report.technical_gaps and not setup.report.technical_gaps[0].depends_on
        setup.service.ontology.update_source_package.assert_not_awaited()
    assert router.await_count == 1


def test_publication_failure_keeps_gap_and_old_query_snapshot(setup):
    setup.service.ontology.update_source_package.side_effect = RuntimeError('transaction failed')
    assert not execute(setup)
    assert setup.run.ontology.version == 'old'
    assert setup.report.technical_gaps
    setup.workflow._ontology_snapshot.assert_not_awaited()


@pytest.mark.parametrize('kind,expected', [('query_relation_repair', 0), ('onboarding', 1)])
def test_partial_query_research_never_prunes_global_structure_inputs(monkeypatch, kind, expected):
    from app import relationship_inputs
    from app.datasource_modeling import ModelingService
    from unittest.mock import Mock
    persist = Mock()
    monkeypatch.setattr(relationship_inputs, 'save_inputs', persist)
    class StopAfterInputPreparation(Exception):
        pass
    def checkpoint():
        raise StopAfterInputPreparation()
    monkeypatch.setattr(relationship_inputs, 'Checkpoints', checkpoint)
    service = ModelingService(SimpleNamespace(), AsyncMock(), repository=SimpleNamespace())
    with pytest.raises(StopAfterInputPreparation):
        asyncio.run(service._semantic_review([], {'a': {}}, {'kind': kind}, discover=False))
    assert persist.call_count == expected
