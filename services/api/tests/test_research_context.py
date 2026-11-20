"""Research context and handoff regressions for the SQL-native workflow."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app import prepared_context
from app.continuation import handoff
from app.ontology_paths import connect_objects
from app.research_tools import ResearchTools
from semora_contracts import QueryRun, ConfirmedFact
from test_sql_native import native_harness


def test_topology_includes_required_bridge_and_rejects_disconnected():
    import pytest
    relations=[{'name':'ab','from':'a','to':'b'},{'name':'bc','from':'b','to':'c'}]
    assert set(connect_objects({'a','c'},relations))=={'ab','bc'}
    with pytest.raises(ValueError):connect_objects({'a','missing'},relations)


def test_keyword_requests_are_independent_and_knowledge_failure_does_not_block(monkeypatch):
    from helpers import harness
    w,session,_,_=harness(monkeypatch)
    run=QueryRun(session_id=session.id,domain='test',question='Query totals')
    _,ont=asyncio.run(w.snapshot_loader('test'));ont['knowledgeBaseIds']=['kb']
    w.model=SimpleNamespace(complete_json=AsyncMock(return_value={'knowledge_keywords':['total','status','total']}))
    search=AsyncMock(side_effect=[RuntimeError('offline'),[]])
    monkeypatch.setattr(prepared_context.knowledge_service,'_wiki_search',search)
    saved=asyncio.run(prepared_context.prepare_context(w,run,run.question,ont,{}))
    assert [c.args for c in search.await_args_list]==[('kb','total'),('kb','status')]
    assert saved['packet']['selected_objects']==['item']
    assert saved['packet']['knowledge']==[]
    assert 'knowledge_warning' in saved['packet']


def test_verbatim_excerpt_preserves_matching_constraints_and_marks_partial():
    text='Introduction\n\n'+('Unrelated paragraph '*200)+'\n\nRequired status = 2.\n\nOther notes'
    value=prepared_context.knowledge_excerpt(text,['status'],limit=200)
    assert 'Required status = 2.' in value['content'] and value['excerpted']
    assert value['original_chars']==len(text)


def test_research_page_is_available_to_sql_role_without_second_search(monkeypatch):
    from app.sql_authoring import material
    w,session,_,_=__import__('helpers').harness(monkeypatch)
    run=QueryRun(session_id=session.id,domain='test',question='Query records')
    state={'packet':{'objects':[],'knowledge':[]}}
    run.agent_memory['prepared_context']=[state]
    tools=ResearchTools(w,run,{'knowledgeBaseIds':['kb']},{})
    tools.context_state=state
    page={'slug':'definitions','title':'Definitions','content':'A valid record has status = 2.'}
    tools.remember_page('kb',page);tools.remember_page('kb',page)
    assert len(state['packet']['knowledge'])==1
    assert material(run)['knowledge'][0]['content']==page['content']
    assert run.agent_memory['model_evidence_refs']


def test_value_probe_checks_scope_and_reports_incomplete_results(monkeypatch):
    from app import field_value_probe
    from semora_contracts import AuthorizationContext, ExecutionResult

    column = {"name": "Status", "sensitivity": "internal"}
    packet = {
        "objects": [{"object": {"name": "item", "table": "public.items", "dataSourceId": "source",
                                "properties": [{"name": "status", "column": "Status"}]}}],
        "schemas": [{"object": "item", "tables": [{"columns": [column]}]}],
    }
    source = SimpleNamespace(scan_result={"capabilities": {"query": "verified"}})
    monkeypatch.setattr(field_value_probe.datasource_service.repository, "get", lambda _: source)
    monkeypatch.setattr(field_value_probe, "dialect_for", lambda _: "postgres")
    execute = AsyncMock(return_value=ExecutionResult(
        columns=["Status"], rows=[["open"], ["unknown+"], [None], ["x" * 81]],
        row_count=4, duration_ms=1,
    ))
    run = QueryRun(session_id="session", domain="test", question="Which statuses exist?",
                   authorization=AuthorizationContext(subject="user", username="user",
                                                      allowed_data_source_ids=["source"]))
    toolkit = ResearchTools(SimpleNamespace(execute=execute, emit=lambda *args: None), run, {}, {})
    toolkit.context_state = {"packet": packet}

    async def probe():
        result = await toolkit.inspect_field_values("item", "status", "Verify the status labels.")
        duplicate = await toolkit.inspect_field_values("item", "status", "Verify again.")
        return result, duplicate

    result, duplicate = asyncio.run(probe())
    assert result == duplicate
    assert result["values"] == ["open", "unknown+", None]
    assert result["complete"] is False
    execute.assert_awaited_once()
    assert execute.call_args.args[0] == 'SELECT DISTINCT "Status" FROM "public"."items" ORDER BY "Status" LIMIT 129'
    assert execute.call_args.args[3]["max_result_rows"] == 128

    import pytest

    column["sensitivity"] = "restricted"
    with pytest.raises(ValueError, match="敏感字段"):
        asyncio.run(field_value_probe.inspect_values(toolkit.workflow, run, {}, packet, "item", "status"))
    execute.assert_awaited_once()


def test_numeric_grouping_is_confirmed_only_for_an_undefined_observed_dimension():
    from app.field_value_probe import numeric_grouping_question

    report = SimpleNamespace(questions=[], technical_gaps=[],
                             logical_plan=SimpleNamespace(grain_refs=["ticket.wait"]),
                             requirements=SimpleNamespace(dimensions=["等待时长组"]))
    evidence = {"object": "ticket", "field": "wait", "complete": True,
                "values": [*map(str, range(40)), "40+"]}
    packet = {"field_value_observations": [evidence],
              "objects": [{"object": {"name": "ticket", "properties": [{"name": "wait"}]}}]}

    question = numeric_grouping_question("找人数最多的等待时长组", report, packet)
    assert question.blocking
    assert [option.id for option in question.options] == ["individual", "range"]
    assert numeric_grouping_question("每10分钟一组，找人数最多的等待时长组", report, packet) is None
    evidence["complete"] = False
    assert numeric_grouping_question("找人数最多的等待时长组", report, packet) is None


def test_context_adds_bridge_without_loading_unrelated_objects(monkeypatch):
    from helpers import harness
    w,session,_,_=harness(monkeypatch)
    run=QueryRun(session_id=session.id,domain='test',question='Query linked records')
    ont={'objects':[{'name':n,'table':n,'properties':[]} for n in ['a','b','c','other']],
         'relations':[{'name':'ab','from':'a','to':'b'},{'name':'bc','from':'b','to':'c'}]}
    packet=prepared_context.context_packet(w,run,ont,['a','c'])
    assert packet['selected_objects']==['a','b','c']


def test_free_text_confirmation_retains_business_facts_across_retry(monkeypatch):
    from semora_contracts import RunStatus
    w,session,*_=native_harness(monkeypatch)
    source=QueryRun(session_id=session.id,domain='test',question='Query items',status=RunStatus.FAILED)
    source.user_answers=[ConfirmedFact(question_id='scope',field='scope',value='Only current records',label='Only current records')]
    source.agent_memory['analyst']=[{'type':'ai','data':{'content':'PRIVATE RAW MEMORY'}}]
    data=handoff(source,session)
    assert data['confirmed_facts'][0]['value']=='Only current records'
    assert 'PRIVATE RAW MEMORY' not in str(data)
    assert not {'task_memory','candidate_intent','review_feedback'} & data.keys()


def test_current_models_keep_historical_display_data_without_restoring_old_execution():
    from app.control_state import restore_run
    run=restore_run({'session_id':'s','question':'Old record','plan':{'domain':'x','intent':'old','metrics':['total'],'stages':[]},
        'review':{'decision':'approved','rationale':'historic'}})
    assert run.plan.sql_program is None and run.plan.model_extra['metrics']==['total']
    assert run.review['decision']=='approved'


def test_retired_compiler_and_policy_routes_are_not_importable():
    import importlib.util
    from app.main import app
    for module in ['app.agentic','app.planner','app.gate','app.audit','semora_agent.roles']:
        assert importlib.util.find_spec(module) is None
    assert not any(path.startswith(('/api/v1/audits','/api/v1/approvals')) for path in app.openapi()['paths'])


def test_source_readback_survives_isolated_sql_role_context(monkeypatch):
    from helpers import harness
    from app.sql_authoring import material
    w,session,_,_=harness(monkeypatch)
    run=QueryRun(session_id=session.id,domain='test',question='Query records')
    state={'packet':{'objects':[],'knowledge':[]}}
    run.agent_memory['prepared_context']=[state]
    run.agent_memory['knowledge_discovery']=[{'kb_id':'kb','kind':'source','ref':'doc'}]
    provider=SimpleNamespace(provider='weknora',_request=AsyncMock(return_value=([{'id':'c1','content':'Only confirmed entries count.'}],{})))
    research=ResearchTools(w,run,{'knowledgeBaseIds':['kb']},{},knowledge_provider=provider)
    research.context_state=state
    read=research.knowledge_tools()[2]
    observed=asyncio.run(read('doc',reason='Verify the required counting rule'))
    assert observed['chunks'][0]['id']=='c1'
    assert material(run)['knowledge'][0]['content']=='Only confirmed entries count.'
    refs=run.agent_memory['model_evidence_refs'][-1]
    assert any({'document_id':'doc','chunk_id':'c1'} in ref.get('source_refs',[]) for ref in refs.values())
