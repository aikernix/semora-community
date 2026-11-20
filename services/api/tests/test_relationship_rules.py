import copy
import pytest
from app.relationship_normalization import join_signature
from app.relationship_rules import REFERENCE_RULES


def relation():
    return {'id': 'r', 'source_datasource_id': 'ds', 'source_table_id': 'records',
            'target_datasource_id': 'ds', 'target_table_id': 'registry',
            'key_pairs': [{'source': 'owner_ref', 'target': 'key'}],
            'condition_template': {'guards': [{'side': 'source', 'field': 'kind', 'value': 'entity'}]}}


def test_relation_identity_does_not_depend_on_model_wording_or_reverse_direction():
    a = relation(); a['query_role'] = 'owner'
    b = copy.deepcopy(a); b['query_role'] = 'entity_reference'
    b.update(source_table_id='registry', target_table_id='records',
             key_pairs=[{'source': 'key', 'target': 'owner_ref'}],
             condition_template={'guards': [{'side': 'target', 'field': 'kind', 'value': 'entity'}]})
    assert join_signature(a) == join_signature(b)
    b['condition_template']['guards'][0]['value'] = 'different_entity'
    assert join_signature(a) != join_signature(b)
    b = copy.deepcopy(a); b['key_pairs'][0]['source'] = 'creator_ref'
    assert join_signature(a) != join_signature(b)


def test_same_join_with_different_role_words_is_one_query_edge():
    from app.query_relationships import deduplicate_query_edges
    a = {**relation(), 'status': 'accepted', 'evidence_grade': 'strong', 'evidence': [], 'query_role': 'owner'}
    b = {**copy.deepcopy(a), 'id': 's', 'query_role': 'lookup'}
    deduplicate_query_edges([a, b])
    assert [c['status'] for c in (a, b)] == ['accepted', 'rejected']


def test_discovery_and_both_review_stages_share_the_same_reference_rules():
    from app.relationship_discovery import SYSTEM, COMPLETION_SYSTEM
    from app.relationship_research import SYSTEM as REVIEW, REVIEW_SYSTEM
    from app.relationship_keywords import AGENT_SYSTEM
    for prompt in (SYSTEM, COMPLETION_SYSTEM, REVIEW, REVIEW_SYSTEM, AGENT_SYSTEM):
        assert prompt.count(REFERENCE_RULES) == 1


def test_all_relationship_prompts_define_role_task_and_finish_with_output_contract():
    import json
    from app.relationship_discovery import READ_SYSTEM, EXPAND_SYSTEM, SYSTEM, COMPLETION_SYSTEM
    from app.relationship_keywords import AGENT_SYSTEM
    from app.relationship_research import SYSTEM as REVIEW, REVIEW_SYSTEM
    from app.relationship_output_schema import MARKER
    for prompt in (READ_SYSTEM,EXPAND_SYSTEM,SYSTEM,COMPLETION_SYSTEM,AGENT_SYSTEM,REVIEW,REVIEW_SYSTEM):
        assert prompt.startswith('【角色】')
        assert '【任务】' in prompt or '【目标】' in prompt
        assert any(header in prompt for header in ('【输入】','【资料】'))
        assert prompt.count(MARKER)==1
        schema=json.loads(prompt.split(MARKER,1)[1].split('\n',1)[1])
        assert schema['type']=='object'
    assert '【可用工具】' in AGENT_SYSTEM and '【交付方式】' in AGENT_SYSTEM
    assert '【复核重点】' in REVIEW_SYSTEM


def test_compact_comparison_has_minimal_output_and_short_reason():
    from app.relationship_contracts import ProposalOutput
    from app.relationship_keywords import COMPARE_SYSTEM
    assert COMPARE_SYSTEM.startswith('【角色】') and '【任务】' in COMPARE_SYSTEM
    assert '$defs' not in COMPARE_SYSTEM and 'keywords' not in COMPARE_SYSTEM and 'gaps' not in COMPARE_SYSTEM
    assert '不超过60个字符' in COMPARE_SYSTEM
    assert set(ProposalOutput.model_fields)=={'relations'}
    reason=ProposalOutput.model_json_schema()['$defs']['ProposedRelation']['properties']['reason']
    assert 'maxLength' not in reason and reason['minLength']==1


def test_relationship_reason_length_is_guidance_only_and_never_truncated():
    from app.relationship_contracts import ProposedRelation, AgentConclusion, CompactDecision, EvidenceGap
    from app.relationship_research import normalize_research_response
    reason='完整说明字段含义及关联判断。'*100
    relation={'source_table_id':'a','target_table_id':'b',
        'key_pairs':[{'source':'ref','target':'id'}],'reason':reason}
    assert ProposedRelation.model_validate(relation).reason==reason
    assert AgentConclusion.model_validate({'relations':[relation],'checked_table_ids':['a','b'],'gaps':[]}).relations[0].reason==reason
    decision={'candidate_id':'candidate','outcome':'needs_evidence','kind':'unknown',
        'gaps':[{'reason':reason,'request':'profile'}]}
    normalized=normalize_research_response({'decisions':[decision]})
    assert CompactDecision.model_validate(normalized['decisions'][0]).reason==reason
    assert EvidenceGap.model_validate({'reason':reason}).reason==reason


def test_data_guidance_is_inserted_before_review_output_schema():
    from app.relationship_research import SYSTEM, with_data_guidance
    from app.relationship_output_schema import MARKER, DATA_RESEARCH_SCHEMA, RESEARCH_SCHEMA, output_format
    prompt=with_data_guidance(SYSTEM)
    assert '【数据核查要求】' not in SYSTEM
    assert prompt.index('【数据核查要求】')<prompt.index(MARKER)
    assert prompt.endswith(output_format(DATA_RESEARCH_SCHEMA))
    assert SYSTEM.endswith(output_format(RESEARCH_SCHEMA))


def test_published_records_keep_own_evidence_and_omit_research_neighbors():
    from decimal import Decimal
    from app.relation_output import published_relationship
    original = {**relation(), 'evidence': [{'kind': 'value_profile', 'statistics': {'matched_keys': 3}}],
                'other_proposals': [{'id': 'unapproved'}], 'target_domain_observations': [{'source_table': 'another'}],
                'research_key': 'checkpoint-internal', 'confidence': Decimal('0.98')}
    record = published_relationship(original)
    assert record['evidence'] == original['evidence']
    assert 'other_proposals' not in record and 'research_key' not in record
    assert 'confidence' not in record
    assert original['confidence'] == Decimal('0.98')
    record['evidence'][0]['statistics']['matched_keys'] = 9
    assert original['evidence'][0]['statistics']['matched_keys'] == 3


def test_published_records_strip_nested_legacy_scores_before_json_encoding():
    import json
    from decimal import Decimal
    from app.relation_output import published_relationship
    original = {'confidence': Decimal('0.9'), 'evidence': [
        {'confidence': Decimal('0.8'), 'coverage': 0.85, 'key_pairs': [{'source': 'a', 'target': 'b'}]}]}
    record = published_relationship(original)
    assert json.loads(json.dumps(record)) == {'evidence': [
        {'coverage': 0.85, 'key_pairs': [{'source': 'a', 'target': 'b'}]}]}
    assert 'confidence' in original['evidence'][0]


@pytest.mark.parametrize('changes', [
    {'missing_evidence': ['identifier domain is not established']},
    {'relationship_kind': 'shared_entity'},
    {'target_role': 'identity'},
    {'structure_sufficient': False},
    {'alternative_unresolved': True},
])
def test_approval_cannot_contradict_its_own_reference_definition(changes):
    from app.relationship_research import decision
    from test_relationship_research import verdict
    assert decision({**verdict('r'), **changes}, strict=True) is None
