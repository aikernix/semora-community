import asyncio
import json
import pytest
from app.relationship_normalization import orient_reference, structural_families, expand_family_leads
from app.relationship_discovery import Discovery, COMPLETION_SYSTEM
from app.relationship_context import request_size, REQUEST_LIMIT
from app.relationship_inputs import digest


def table(name, fields, primary=(), unique=()):
    return {'table_id': name, 'datasource_id': 'ds', 'name': name, 'comment': '', 'kind': 'TABLE',
            'columns': [{'name': f, 'type': 'integer', 'comment': 'identifier'} for f in fields],
            'primary_keys': [{'columns': list(primary)}] if primary else [],
            'unique_constraints': [{'columns': list(k)} for k in unique]}


def test_direction_uses_complete_unique_keys_and_reverses_guards():
    cards = {'a': table('a', ['x', 'scope'], ['x']), 'b': table('b', ['ref', 'scope'])}
    p = {'source_table_id': 'a', 'target_table_id': 'b', 'key_pairs': [{'source': 'x', 'target': 'ref'}],
         'guards': [{'side': 'target', 'field': 'scope', 'operator': 'eq', 'value': 1}]}
    result = orient_reference(p, cards)
    assert result['source_table_id'] == 'b' and result['target_table_id'] == 'a'
    assert result['key_pairs'] == [{'source': 'ref', 'target': 'x'}]
    assert result['guards'][0]['side'] == 'source'
    cards['a']['primary_keys'] = [{'columns': ['x', 'scope']}]
    assert orient_reference(p, cards) == p  # a composite prefix is not unique


def test_filtered_or_disabled_unique_indexes_do_not_prove_global_uniqueness():
    from app.datasource_modeling import _indexed
    t = {'indexes': [{'columns': ['code'], 'unique': True, 'predicate': 'active = 1'}]}
    assert not _indexed(t, ['code'], unique=True)
    t['indexes'][0] = {'columns': ['code'], 'unique': True, 'is_valid': False}
    assert not _indexed(t, ['code'], unique=True)
    t['indexes'][0] = {'columns': ['code'], 'unique': True}
    assert _indexed(t, ['scope', 'code'], unique=True)


@pytest.mark.parametrize('prefix', ['events', 'registry_log', '数据分片'])
def test_family_expansion_is_a_lead_and_never_infers_target_partition_routing(prefix):
    cards = {n: table(n, ['id', 'ref'], ['id']) for n in [prefix, prefix+'_0', prefix+'_1']}
    cards['other'] = table('other', ['id'], ['id'])
    cards[prefix+'_2'] = table(prefix+'_2', ['id', 'ref', 'scope'], ['id'])
    assert len(structural_families(cards)[0]) == 3
    p = {'source_table_id': prefix+'_0', 'target_table_id': 'other', 'key_pairs': [{'source': 'ref', 'target': 'id'}]}
    result = expand_family_leads([p], cards)
    assert {p['source_table_id'] for p in result} == {prefix, prefix+'_0', prefix+'_1'}
    assert all('status' not in p for p in result)
    incoming = {**p, 'source_table_id': 'other', 'target_table_id': prefix+'_0'}
    assert expand_family_leads([incoming], cards) == [incoming]
    assert len(expand_family_leads([p, {**p, 'source_table_id': prefix+'_1', 'reason': 'another observation'}], cards)) == 3


def test_reference_audit_keeps_source_fields_and_model_search_targets_without_pair_rescan():
    cards = {'source': table('source', ['owner_ref']), 'registry': table('registry', ['opaque_key'], ['opaque_key'])}
    cards['registry']['comment'] = 'entity catalog'
    state = {}
    d = Discovery(cards, None, state, lambda: None)
    state['lead_journal'] = {d.input_hash: {'lead': {'depth': 0, 'lead': {
        'source_table_id': 'source', 'fields': ['owner_ref'], 'question': 'owner identity', 'keywords': ['entity']}}}}
    tasks = list(d.reference_tasks())
    assert tasks and all(request_size(COMPLETION_SYSTEM, p) <= REQUEST_LIMIT for p in tasks)
    assert all('comparison_pairs' not in p for p in tasks)
    assert any(l['fields'] == ['owner_ref'] for p in tasks for l in p['focus_leads'])
    p = tasks[0]
    response = {'relations': [{'source_table_id': 'source', 'target_table_id': 'registry',
        'key_pairs': [{'source': 'owner_ref', 'target': 'opaque_key'}]}], 'leads': [], 'complete': True}
    assert d.validate_response(response, p)['relations']


def test_partial_positive_reference_remains_unresolved_despite_independent_semantics():
    from app.relationship_evidence import finalize_relationships
    from test_relationship_research import candidate, verdict
    c = candidate('reference')
    v = {**verdict(c['id']), 'structure_sufficient': True}
    c['evidence'].extend([{'kind': 'raw_schema_context', 'source_description': 'entity reference', 'target_description': 'entity key'},
        {'kind': 'value_profile', 'statistics': {'source_nonnull_distinct_keys': 7, 'source_exhausted': False,
                                               'matched_keys': 5, 'directional_coverage': 5/7}},
        {'kind': 'model_semantic_review', **v}, {'kind': 'independent_review', **v}])
    finalize_relationships([c])
    assert c['status'] == 'unresolved'
    assert c['evidence'][-1]['proof_path'] == 'insufficient'
    c['evidence'] = [e for e in c['evidence'] if e['kind'] != 'independent_review']
    finalize_relationships([c])
    assert c['status'] == 'unresolved'


def test_domain_observations_are_facts_and_do_not_copy_another_models_verdict():
    from app.relationship_domain_audit import domain_observations
    c = {'source_datasource_id': 'ds', 'source_table_id': 'events', 'source_columns': ['ref'],
         'target_datasource_id': 'ds', 'target_table_id': 'registry', 'target_columns': ['key'],
         'evidence': [{'kind': 'raw_schema_context', 'source_description': 'external identifier'},
             {'kind': 'value_profile', 'statistics': {'source_nonnull_distinct_keys': 20, 'matched_keys': 0}},
             {'kind': 'model_semantic_review', 'supported': True}]}
    observations = domain_observations([c, {**c, 'id': 'another-role'}])
    values = observations[('ds', 'registry', ('key',))]
    assert len(values) == 1
    assert values[0]['matched_keys'] == 0
    assert 'supported' not in json.dumps(values)


def test_zero_matches_cannot_be_overridden_by_incomplete_data_claim():
    from app.relationship_evidence import finalize_relationships
    from test_relationship_research import candidate, verdict
    c = candidate('defined')
    v = {**verdict(c['id']), 'structure_sufficient': True, 'coverage_assessment': 'incomplete_reference_data'}
    c['evidence'].extend([{'kind': 'raw_schema_context', 'source_description': 'original record key', 'target_description': 'record key'},
        {'kind': 'value_profile', 'statistics': {'source_nonnull_distinct_keys': 1, 'matched_keys': 0, 'directional_coverage': 0}},
        {'kind': 'model_semantic_review', **v}, {'kind': 'independent_review', **v}])
    finalize_relationships([c])
    assert c['status'] == 'unresolved'
    c['evidence'][-2]['coverage_assessment'] = 'unknown'  # remove independent explanation
    finalize_relationships([c])
    assert c['status'] == 'unresolved'


def test_large_reference_directory_is_paged_without_truncating_model_search_hits():
    cards = {str(i): table('table_'+str(i), ['id', 'ref'], ['id']) for i in range(1000)}
    for identifier, c in cards.items():
        c['table_id'] = identifier
    state = {}; d = Discovery(cards, None, state, lambda: None)
    state['lead_journal'] = {d.input_hash: {'lead': {'depth': 0, 'lead': {
        'source_table_id': '0', 'fields': ['ref'], 'question': 'reference', 'keywords': ['table']}}}}
    tasks = list(d.reference_tasks())
    assert 1 < len(tasks) < 10
    assert all(len(t['tables']) <= 20 and request_size(COMPLETION_SYSTEM, t) <= REQUEST_LIMIT for t in tasks)
    assert {c['table_id'] for t in tasks for c in t['catalog']} | {c['table_id'] for t in tasks for c in t['tables']} == set(cards)


def test_open_leads_do_not_discard_accounted_reference_fields_or_claim_pair_coverage():
    from app.relationship_discovery import DiscoveryIncomplete
    cards = {'source': table('source', ['ref', 'external_ref']), 'target': table('target', ['id'], ['id'])}
    d = Discovery(cards, None, {}, lambda: None)
    p = {'phase': 'complete_references', 'tables': list(cards.values()),
         'focus_leads': [{'source_table_id': 'source', 'fields': ['ref', 'external_ref']}]}
    r = {'complete': False, 'relations': [{'source_table_id': 'source', 'target_table_id': 'target',
         'key_pairs': [{'source': 'ref', 'target': 'id'}]}], 'leads': [{'source_table_id': 'source',
         'fields': ['external_ref'], 'question': 'unavailable definition', 'keywords': []}]}
    checked = d.validate_response(r, p)
    assert checked['reference_coverage'] == {'required': 2, 'accounted': 2, 'model_complete': False}
    assert checked['complete'] is False and checked['leads'] == r['leads']
    assert not d.state.get('completed_pairs')
    with pytest.raises(DiscoveryIncomplete, match='incomplete_reference_field_coverage') as failure:
        d.validate_response({**r, 'leads': []}, p)
    assert failure.value.details['missing_reference_fields'] == [{'source_table_id': 'source', 'field': 'external_ref'}]
    with pytest.raises(DiscoveryIncomplete, match='incomplete_reference_field_coverage'):
        d.validate_response({**r, 'leads': [], 'complete': True}, p)
    with pytest.raises(DiscoveryIncomplete, match='incomplete_discovery_response'):
        d.validate_response(r, {**p, 'phase': 'compare'})


def test_enum_context_reads_only_explicit_small_code_definitions():
    from app.relationship_enum_context import declared_enums
    t = {'columns': [{'name': 'kind', 'comment': '1:PRIMARY 2:SECONDARY 3:OTHER'},
                     {'name': 'credential', 'comment': 'secret credential'},
                     {'name': 'record_id', 'comment': 'record identifier'}]}
    assert declared_enums(t) == {'kind': [1, 2, 3]}


def test_new_sampling_facts_invalidate_the_structural_completion_and_request_review():
    from app.relationship_research_loop import audit_structural_acceptances
    from app.relationship_research import VERSION
    from app.relationship_evidence import finalize_relationships
    from test_relationship_research import candidate, verdict
    c = candidate('fresh-facts')
    c['status'] = 'accepted'
    c['evidence'].extend([{'kind': 'model_semantic_review', **verdict(c['id'])},
                          {'kind': 'research_complete', 'version': VERSION}])
    async def fetch(*args):
        return {'kind': 'value_profile', 'statistics': {'source_nonnull_distinct_keys': 1, 'matched_keys': 1,
                'directional_coverage': 1, 'target_context': {'kind': {'2': 1}}}}
    async def persist(values):
        finalize_relationships(values)
    again = asyncio.run(audit_structural_acceptances([c], fetch, persist))
    assert again == [c]
    assert not any(e.get('kind') == 'research_complete' for e in c['evidence'])
    assert asyncio.run(audit_structural_acceptances([c], fetch, persist)) == []


@pytest.mark.parametrize('dialect', ['mysql', 'postgres', 'oracle', 'tsql'])
def test_observed_key_proof_checks_full_mapping_and_target_scope_with_limits(dialect):
    from app.relationship_key_probe import key_probe_queries
    from sqlglot import exp
    queries = key_probe_queries('schema.records', ['scope', 'code'], [
        {'side': 'target', 'field': 'category', 'value': 'active'},
        {'side': 'source', 'field': 'unrelated_source_flag', 'value': 1}], dialect)
    assert len(queries) == 2
    for q in queries:
        assert q.find(exp.Limit)
        text = q.sql(dialect=dialect)
        assert 'category' in text and "'active'" in text
        assert 'unrelated_source_flag' not in text
    assert len(queries[0].find(exp.Group).expressions) == 2
    assert not queries[1].find(exp.Count)  # existence, not an unbounded second full count
