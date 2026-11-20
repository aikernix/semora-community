import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from semora_contracts import QueryRun, QueryPlan, SQLProgram, SQLArtifact, ExecutionResult, RunStatus
from app.sql_execution import SQLExecution
from app.executor_errors import ExecutorOperationError


def test_second_statement_has_its_own_deadline_and_keeps_first_result(monkeypatch):
    monkeypatch.setattr('app.sql_execution.EXECUTOR_HTTP_GRACE_SECONDS', 0)

    async def scenario():
        cancelled=[]
        async def execute(sql,*args):
            if sql=='SELECT 1 AS id':
                return ExecutionResult(columns=['id'],rows=[[1]],row_count=1,duration_ms=0)
            try:await asyncio.Event().wait()
            finally:cancelled.append(True)
        program=SQLProgram(summary='Read both sources',steps=[
            {'name':'first','purpose':'Read first source','data_source_id':'a','sql':'SELECT 1 AS id','output_columns':['id']},
            {'name':'second','purpose':'Read second source','data_source_id':'b','sql':'SELECT id FROM records','output_columns':['id']},
        ])
        run=QueryRun(session_id='s',question='Read records',status=RunStatus.EXECUTING,
                     plan=QueryPlan(domain='test',intent='Read records',sql_program=program))
        w=SimpleNamespace(execute=AsyncMock(side_effect=execute),emit=Mock(),store=SimpleNamespace(save_run=Mock()))
        execution=SQLExecution(w,run,{}, {'query_timeout_seconds':0.01})
        execution.artifact=lambda s,results:SQLArtifact(semantic_sql=s.sql,physical_sql=s.sql,
             data_source_id=s.data_source_id,artifact_hash='test',compilation={'data_source_label':s.data_source_id})
        monkeypatch.setattr('app.sql_learning.record_sql_success',lambda *args:None)
        with pytest.raises(ExecutorOperationError) as caught:await execution.execute()
        assert caught.value.public()['timeout_origin']=='system' and cancelled==[True,True]
        assert w.execute.await_count==3 and run.source_results['first'].row_count==1
        retries=[call for call in w.emit.call_args_list if call.args[1]=='source.step.retry']
        assert len(retries)==1 and retries[0].args[3]['step']=='second'
        failure=run.agent_memory['execution_failure'][-1]
        assert failure['step_index']==2 and failure['data_source_id']=='b'
        assert failure['sql']=='SELECT id FROM records' and run.result is None
    asyncio.run(scenario())
