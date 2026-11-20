import asyncio

from app.relationship_evidence import finalize_relationships
from app.relationship_research import research, batches


def test_model_evidence_excludes_internal_decisions_and_old_model_opinions():
    from app.relationship_research import factual_evidence
    facts = [{"kind": "explicit_field_reference"}, {"kind": "target_unique_index", "confirmed": True},
             {"kind": "value_profile", "statistics": {"directional_coverage": .7}}]
    opinions = [{"kind": kind} for kind in ("join_contract", "confirmation", "automatic_exclusion",
                "model_semantic_review", "independent_review", "competition", "research_complete")]
    assert factual_evidence(facts + opinions) == facts


def test_resume_restores_complete_join_and_checks_both_schema_hashes():
    from app.relationship_research import restore_checkpoint, VERSION
    original = {"source_schema_hash": "a", "target_schema_hash": "b", "status": "accepted",
                "condition_template": {"guards": [{"side": "source", "field": "kind", "operator": "eq", "value": "entity"}]},
                "evidence": [{"kind": "research_complete", "version": VERSION}]}
    restored = {}
    assert not restore_checkpoint(restored, original, "a", "changed")
    assert not restored
    assert restore_checkpoint(restored, original, "a", "b")
    assert restored["condition_template"] == original["condition_template"]


def candidate(identifier):
    return {"id": identifier, "status": "pending", "evidence_grade": "medium", "confidence": .7,
            "evidence": [{"kind": "target_unique_index", "confirmed": True}]}


def verdict(identifier):
    return {"candidate_id": identifier, "supported": True, "confidence": .97,
            "structure_sufficient": True,
            "target_role": "reference", "alternative_unresolved": False, "reason": "supported by evidence",
            "key_domain": "same", "relationship_kind": "entity_reference", "coverage_expectation": "complete",
            "query_outcome": "usable", "exclusion_code": "", "missing_evidence": [],
            "query_role": "entity reference", "additional_predicates": []}


async def verified_profile(*args):
    direction={'source_nonnull_distinct_keys':100,'matched_keys':100,'directional_coverage':1}
    return {'kind':'value_profile','statistics':{'method':'bidirectional_random_membership_v4',
            **direction,'forward':dict(direction),'reverse':dict(direction)}}


def test_all_candidates_are_researched_and_foreign_keys_never_reenter_model_or_tools():
    import json
    values = [candidate(str(i)) for i in range(130)]
    foreign = {**candidate("foreign"), "evidence_grade": "foreign_key", "status": "accepted"}
    values.append(foreign)
    calls, fetched, saved = [], [], []

    class Model:
        async def complete_json(self, system, user):
            packets = json.loads(user)["candidates"]
            ids = [p["candidate_id"] for p in packets]
            assert "foreign" not in ids
            from app.relationship_context import request_size, REQUEST_LIMIT
            assert request_size(system, json.loads(user)) <= REQUEST_LIMIT
            calls.extend(ids)
            if "通过requests" in system and not all(any(e["kind"] == "value_profile" for e in p["evidence"]) for p in packets):
                return {"requests": [{"candidate_id": i, "kind": "profile"} for i in ids], "decisions": []}
            return {"decisions": [verdict(i) for i in ids]}

    async def fetch(c, kind):
        fetched.append((c["id"], kind))
        return {"kind": "value_profile", "statistics": {"source_nonnull_distinct_keys": 100, "directional_coverage": 1}}

    async def persist(entries):
        finalize_relationships(entries)
        saved.extend(c["id"] for c in entries)

    async def progress(done, total):
        assert done <= total == 130

    packet = lambda c: {"candidate_id": c["id"], "evidence": c["evidence"]}
    async def execute():
        await research(values, packet, fetch, Model(), persist, progress)
        from app.relationship_research_loop import audit_structural_acceptances
        await audit_structural_acceptances(values, fetch, persist)
    asyncio.run(execute())
    assert all(c["status"] == "accepted" for c in values)
    assert len(fetched) == 130
    assert set(saved) == {str(i) for i in range(130)}
    previous = len(calls)
    asyncio.run(research(values, packet, fetch, Model(), persist, progress))
    assert len(calls) == previous  # completed batches resume without another request


def test_documented_reference_still_requires_sample_coverage():
    c = candidate("documented")
    c["evidence"] = [{"kind": "explicit_field_reference"},
                     {"kind": "model_semantic_review", **verdict("documented")},
                     {"kind": "independent_review", **verdict("documented")}]
    finalize_relationships([c])
    assert c["status"] == "unresolved"
    assert 'value_verification_missing' in c['evidence'][-1]['reasons']
    c["evidence"].append({"kind": "value_profile", "statistics": {
        "source_nonnull_distinct_keys": 100, "directional_coverage": 0}})
    finalize_relationships([c])
    assert c["status"] == "unresolved"


def test_restored_structural_acceptance_is_audited_before_publication():
    from app.relationship_research_loop import audit_structural_acceptances
    c = candidate('restored')
    c['evidence'].extend([{'kind': 'entity_key_name'},
        {'kind': 'model_semantic_review', **verdict(c['id'])},
        {'kind': 'review_policy', 'required': False}])
    c['status'] = 'accepted'  # Historical published record, before revalidation.
    foreign = {**candidate('foreign'), 'status': 'accepted', 'evidence_grade': 'foreign_key'}
    calls = []
    async def fetch(value, kind):
        calls.append((value['id'], kind))
        return {'kind': 'value_profile', 'statistics': {
            'source_nonnull_distinct_keys': 19, 'source_exhausted': True, 'directional_coverage': 1 / 19}}
    async def persist(values):
        finalize_relationships(values)
    asyncio.run(audit_structural_acceptances([c, foreign], fetch, persist))
    assert c['status'] == 'unresolved'
    assert foreign['status'] == 'accepted'
    asyncio.run(audit_structural_acceptances([c, foreign], fetch, persist))
    assert calls == [('restored', 'profile')]


def test_failed_acceptance_audit_is_explicitly_incomplete():
    from app.relationship_research_loop import audit_structural_acceptances
    c = {**candidate('failed-audit'), 'status': 'accepted'}
    async def fetch(*args):
        raise TimeoutError()
    async def persist(values):
        pass
    asyncio.run(audit_structural_acceptances([c], fetch, persist))
    assert c['evidence'][-1] == {'kind': 'tool_unavailable', 'tool': 'profile', 'reason': 'TimeoutError', 'error_message': ''}


def test_context_batches_keep_complete_records_and_bound_bytes():
    packets = [{"candidate_id": str(i), "description": "字" * 1000,
                "key_pairs": [{"source": "a", "target": "b"}, {"source": "c", "target": "d"}]}
               for i in range(25)]
    groups = list(batches(packets, max_bytes=12000))
    assert sum(len(group) for group, _ in groups) == 25
    assert all(fits and all(len(p["key_pairs"]) == 2 for p in group) for group, fits in groups)


def test_declared_unique_entity_reference_with_independent_semantic_evidence():
    c = candidate("structured")
    c["evidence"].extend([
        {"kind": "entity_key_name"},
        {"kind": "model_semantic_review", **verdict("structured")},
        {"kind": "independent_review", **verdict("structured")}])
    finalize_relationships([c])
    assert c["status"] == "unresolved"
    assert 'value_verification_missing' in c['evidence'][-1]['reasons']
    c["evidence"].append({"kind": "value_profile", "statistics": {
        "source_nonnull_distinct_keys": 100, "directional_coverage": .2}})
    finalize_relationships([c])
    assert c["status"] == "unresolved"


def test_missing_structured_decisions_are_repaired_without_losing_candidate():
    class Model:
        calls = 0
        async def complete_json(self, system, user):
            self.calls += 1
            if self.calls == 1:
                return {"decisions": []}
            return {"decisions": [verdict("repair")]}
    c = candidate("repair")
    async def fetch(c, kind):
        return {"kind": "value_profile", "statistics": {"source_nonnull_distinct_keys": 100, "directional_coverage": 1}}
    async def persist(entries):
        finalize_relationships(entries)
    async def progress(done, total):
        pass
    model = Model()
    asyncio.run(research([c], lambda c: {"candidate_id": c["id"], "evidence": c["evidence"]},
                         fetch, model, persist, progress))
    assert c["status"] == "accepted"
    assert model.calls == 2


def test_uncertain_proposal_gets_independent_review_and_one_bounded_reconciliation():
    import json
    c = candidate("uncertain")
    calls = []
    class Model:
        async def complete_json(self, system, user):
            calls.append(json.loads(user))
            v = verdict("uncertain")
            if len(calls) == 1:
                v.update(supported=False, query_outcome="needs_evidence", key_domain="unknown", missing_evidence=["标识体系"])
            else:
                v.update(supported=False, query_outcome="exclude", key_domain="different", exclusion_code="meaning_mismatch")
            return {"decisions": [v]}
    async def fetch(*args):
        raise AssertionError("明确不同的标识不需要取样")
    async def persist(entries):
        finalize_relationships(entries)
    async def progress(*args):
        pass
    asyncio.run(research([c], lambda c: {"candidate_id": c["id"], "evidence": c["evidence"]}, fetch, Model(), persist, progress))
    assert len(calls) == 3
    assert c["status"] == "rejected"


def test_unknown_identifier_domain_cannot_pass_on_confidence_alone():
    c = candidate("domain")
    v = {**verdict("domain"), "confidence": 1, "key_domain": "unknown"}
    c["evidence"].extend([{"kind": "entity_key_name"}, {"kind": "model_semantic_review", **v}, {"kind": "independent_review", **v}])
    finalize_relationships([c])
    assert c["status"] == "unresolved"
    assert "identifier_domain_unverified" in c["evidence"][-1]["reasons"]


def test_shared_entity_exclusion_has_one_contract_and_is_not_a_format_failure():
    from app.relationship_contracts import ResearchOutput
    from app.relationship_research import decision
    value = {**verdict('shared'), 'supported': False, 'query_outcome': 'exclude',
             'target_role': 'unknown', 'relationship_kind': 'shared_entity', 'exclusion_code': 'shared_entity'}
    ResearchOutput.model_validate({'decisions': [value]})
    assert decision(value, strict=True) is not None
    c = candidate('shared')
    c['evidence'].extend([{'kind': 'model_semantic_review', **value},
                           {'kind': 'review_policy', 'required': False}])
    finalize_relationships([c])
    assert c['status'] == 'rejected'
    assert c['evidence'][-1]['reasons'] == ['shared_entity']


def test_complete_small_source_can_validate_many_rows_to_one_entity():
    c = candidate("small")
    c["evidence"].extend([{"kind": "model_semantic_review", **verdict("small")},
        {"kind": "independent_review", **verdict("small")}, {"kind": "value_profile", "statistics": {
            "source_nonnull_distinct_keys": 50, "source_exhausted": True, "directional_coverage": 1}}])
    finalize_relationships([c])
    assert c["status"] == "accepted"
    assert c["evidence"][-1]["proof_path"] == "verified_values"


def test_clear_structural_reference_requires_profile_but_not_redundant_review():
    c=candidate('clear')
    fetched=[];calls=[]
    class Model:
        async def complete_json(self,system,user):
            calls.append(system)
            return {'decisions':[{**verdict('clear'),'structure_sufficient':True,'reason':'表用途与所属实体编号一致，目标为唯一标识'}]}
    async def fetch(c,kind):
        fetched.append(kind)
        return await verified_profile()
    async def persist(entries):finalize_relationships(entries)
    async def progress(done,total):pass
    asyncio.run(research([c],lambda c:{'candidate_id':c['id'],'evidence':c['evidence']},fetch,Model(),persist,progress))
    assert c['status']=='accepted'
    assert fetched==['profile'] and len(calls)==1
    assert c['review_required'] is False


def test_structural_shortcut_rejects_ambiguity_incomplete_conditions_and_conflicts():
    from app.relationship_review_policy import structure_is_sufficient
    c=candidate('clear')
    d={**verdict('clear'),'structure_sufficient':True,'reason':'引用用途一致'}
    assert not structure_is_sufficient(c,d)
    c['evidence'].append(asyncio.run(verified_profile()))
    assert structure_is_sufficient(c,d)
    assert not structure_is_sufficient(c,{**d,'alternative_unresolved':True})
    assert not structure_is_sufficient(c,{**d,'key_domain':'unknown'})
    assert not structure_is_sufficient(c,{**d,'additional_predicates':[{'field':'kind','value':1}]})
    assert not structure_is_sufficient({**c,'evidence':[]},d)
    c['evidence'].append({'kind':'value_profile','statistics':{'source_nonnull_distinct_keys':10,'directional_coverage':0}})
    assert not structure_is_sufficient(c,d)


def test_evidence_review_three_attempts_preserve_candidate_input_and_add_repair_feedback():
    c=candidate('retry_contract')
    calls=[]
    class Model:
        async def complete_json(self,system,user):
            calls.append((system,user))
            if len(calls)<3:return {'decisions':[]}
            return {'decisions':[{**verdict('retry_contract'),'structure_sufficient':True}]}
    async def fetch(*args):return await verified_profile()
    async def persist(entries):finalize_relationships(entries)
    async def progress(*args):pass
    asyncio.run(research([c],lambda c:{'candidate_id':c['id'],'evidence':c['evidence']},fetch,Model(),persist,progress))
    assert c['status']=='accepted'
    assert len(calls)==3
    assert calls[0][1]==calls[1][1]==calls[2][1]
    assert '上次输出校验失败' in calls[1][0] and '上次输出校验失败' in calls[2][0]


def test_last_review_retry_keeps_valid_decisions_and_marks_only_bad_candidate_pending():
    good=candidate('good_decision');bad=candidate('bad_decision');calls=[]
    class Model:
        async def complete_json(self,system,user):
            calls.append((system,user))
            if len(calls)<=2:return {'decisions':[]}
            return {'decisions':[{**verdict('good_decision'),'structure_sufficient':True},
                                 {'candidate_id':'bad_decision'}]}
    async def fetch(*args):return await verified_profile()
    async def persist(entries):finalize_relationships(entries)
    async def progress(*args):pass
    asyncio.run(research([good,bad],lambda c:{'candidate_id':c['id'],'evidence':c['evidence']},fetch,Model(),persist,progress))
    assert good['status']=='accepted'
    assert bad['status']=='unresolved' and bad['research_pending'] is True
    assert any(e.get('kind')=='research_gap' for e in bad['evidence'])


def test_second_review_retry_keeps_valid_decisions_and_marks_only_bad_candidate_pending():
    good=candidate('second_good');bad=candidate('second_bad');calls=[]
    class Model:
        async def complete_json(self,system,user):
            calls.append((system,user))
            if len(calls)==1:return {'decisions':[]}
            return {'decisions':[{'candidate_id':'second_good','outcome':'usable',
                                  'kind':'entity_reference','reason':'引用明确'},
                                 {'candidate_id':'second_bad'}]}
    async def fetch(*args):return await verified_profile()
    async def persist(entries):finalize_relationships(entries)
    async def progress(*args):pass
    asyncio.run(research([good,bad],lambda c:{'candidate_id':c['id'],'evidence':c['evidence']},fetch,Model(),persist,progress))
    assert len(calls)>=2 and calls[0][1]==calls[1][1]
    assert '上次输出校验失败' in calls[1][0]
    import json
    assert [p['candidate_id'] for p in json.loads(calls[2][1])['candidates']]==['second_bad']
    assert good['status']=='accepted'
    assert bad['status']=='unresolved' and bad['research_pending'] is True
    assert any(e.get('diagnostic',{}).get('error_type')=='ValidationError' for e in bad['evidence'])


def test_third_review_retry_targets_only_invalid_candidate_and_can_recover_it():
    import json
    good=candidate('repair_good');bad=candidate('repair_bad');calls=[]
    class Model:
        async def complete_json(self,system,user):
            ids=[p['candidate_id'] for p in json.loads(user)['candidates']]
            calls.append((system,ids))
            if len(calls)<3:
                return {'decisions':([
                    {'candidate_id':'repair_good','outcome':'usable','kind':'entity_reference','reason':'引用明确'}
                    ] if 'repair_good' in ids else [])+([
                    {'candidate_id':'repair_bad'}] if 'repair_bad' in ids else [])}
            return {'decisions':[{'candidate_id':'repair_bad','outcome':'usable',
                                  'kind':'direct_record_reference','reason':'目标主键与字段用途一致'}]}
    async def fetch(*args):return await verified_profile()
    async def persist(entries):finalize_relationships(entries)
    async def progress(*args):pass
    asyncio.run(research([good,bad],lambda c:{'candidate_id':c['id'],'evidence':c['evidence']},fetch,Model(),persist,progress))
    assert len(calls)==3 and calls[2][1]==['repair_bad']
    assert '上次输出校验失败' in calls[2][0] and 'kind' in calls[2][0]
    assert good['status']==bad['status']=='accepted'
    assert not any(e.get('kind')=='research_gap' for c in (good,bad) for e in c['evidence'])


def test_compact_contract_merges_gap_requests_and_restores_legacy_gaps():
    from app.relationship_contracts import CompactDecision, CompactResearchOutput
    from app.relationship_output_schema import RESEARCH_SCHEMA, DATA_RESEARCH_SCHEMA
    legacy=CompactDecision.model_validate({'candidate_id':'c','outcome':'needs_evidence',
        'kind':'unknown','reason':'缺少取值分布','gaps':['旧格式缺口']})
    assert legacy.gaps[0].reason=='旧格式缺口' and legacy.gaps[0].request is None
    current=CompactDecision.model_validate({'candidate_id':'c','outcome':'needs_evidence',
        'kind':'unknown','reason':'缺少取值分布','gaps':[{'reason':'需要统计','request':'profile'}]})
    assert current.gaps[0].request=='profile'
    assert 'requests' not in RESEARCH_SCHEMA['properties']
    assert 'coverage' not in RESEARCH_SCHEMA['$defs']['CompactDecision']['properties']
    assert 'coverage' in DATA_RESEARCH_SCHEMA['$defs']['DataCompactDecision']['properties']


def test_competing_targets_do_not_skip_data_verification():
    from app.relationship_review_policy import structure_is_sufficient
    c=candidate('competing')
    c.update(source_datasource_id='ds',source_table_id='events',source_columns=['owner_id'],target_datasource_id='ds',target_table_id='entities',target_columns=['id'])
    c['other_proposals']=[{'id':'other','source_datasource_id':'ds','source_table_id':'events','source_columns':['owner_id'],'target_datasource_id':'ds','target_table_id':'other_entities','target_columns':['id']}]
    assert not structure_is_sufficient(c,{**verdict('competing'),'structure_sufficient':True})


def test_partial_coverage_cannot_be_explained_by_two_model_opinions_alone():
    c=candidate('partial')
    d={**verdict('partial'),'coverage_expectation':'partial'}
    c['evidence'] += [{'kind':'raw_schema_context','source_description':'所属实体编号','target_description':'实体编号'},
        {'kind':'model_semantic_review',**d},{'kind':'independent_review',**d},
        {'kind':'value_profile','statistics':{'source_nonnull_distinct_keys':3,'source_exhausted':True,'directional_coverage':.333333}}]
    finalize_relationships([c])
    assert c['status']=='unresolved'


def test_profile_timeout_is_technical_pending_not_zero_match_or_exclusion():
    from app.relationship_research_loop import audit_structural_acceptances
    c=candidate('profile_timeout');c['status']='accepted'
    async def fetch(*args):raise TimeoutError('database statement timeout')
    async def persist(entries):finalize_relationships(entries)
    rechecks=asyncio.run(audit_structural_acceptances([c],fetch,persist))
    assert rechecks==[]
    issue=next(e for e in c['evidence'] if e.get('kind')=='tool_unavailable')
    assert issue['tool']=='profile' and issue['reason']=='TimeoutError'
    assert 'database statement timeout' in issue['error_message']
    assert c['research_pending'] is True
    assert not any(e.get('kind')=='automatic_exclusion' for e in c['evidence'])
    assert not any(e.get('kind')=='value_profile' and e.get('statistics',{}).get('matched_keys')==0 for e in c['evidence'])


def test_previous_publication_is_preserved_on_profile_outage_without_new_approval():
    from app.relationship_research_loop import audit_structural_acceptances
    c=candidate('historical_profile_timeout')
    c['status']='accepted'
    c['evidence'].extend([{'kind':'model_semantic_review',**verdict(c['id'])},
                         {'kind':'review_policy','required':False}])
    async def fetch(*args):raise TimeoutError('database statement timeout')
    async def persist(entries):finalize_relationships(entries)
    assert asyncio.run(audit_structural_acceptances([c],fetch,persist))==[]
    assert c['status']=='accepted' and c['research_pending'] is True
    assert any(e.get('kind')=='tool_unavailable' for e in c['evidence'])


def test_technical_research_gap_is_pending_not_a_semantic_rejection():
    new=candidate('new_pending')
    new['evidence'].append({'kind':'research_gap','reason':'ValidationError'})
    previous=candidate('previous_pending')
    previous['status']='accepted'
    previous['evidence'].append({'kind':'tool_unavailable','reason':'TimeoutError'})
    finalize_relationships([new,previous])
    assert new['status']=='unresolved' and new['research_pending'] is True
    assert previous['status']=='accepted' and previous['research_pending'] is True
    assert not any(e.get('kind')=='automatic_exclusion' for c in (new,previous) for e in c['evidence'])


def test_missing_duplicate_reason_reuses_explicit_evidence_gap_without_mutating_response():
    from app.relationship_research import normalize_research_response
    from app.relationship_contracts import CompactResearchOutput
    original={'decisions':[{'candidate_id':'c','outcome':'needs_evidence','kind':'unknown',
        'gaps':[{'reason':'需要验证编号所属体系','request':'profile'}]}]}
    normalized=normalize_research_response(original)
    parsed=CompactResearchOutput.model_validate(normalized)
    assert parsed.decisions[0].reason=='需要验证编号所属体系'
    assert 'reason' not in original['decisions'][0]
    assert parsed.decisions[0].outcome=='needs_evidence'


def test_coverage_review_uses_eighty_percent_boundary():
    from app.relationship_review_policy import review_reasons
    c=candidate('coverage_boundary')
    for coverage,expected in ((.79,True),(.80,False),(.90,False),(.98,False)):
        c['evidence']=[{'kind':'value_profile','statistics':{
            'source_nonnull_distinct_keys':100,'directional_coverage':coverage}}]
        assert ('data_contradiction' in review_reasons(c,verdict('coverage_boundary'))) is expected
