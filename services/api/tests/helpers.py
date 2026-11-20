"""Query confirmation, SQL validation and automatic read-only execution."""
import json
import copy
import sqlite3
import re
import sqlglot
from unittest.mock import AsyncMock

from semora_contracts import OntologySnapshotRef, ExecutionResult
from app.store import Store
from app.workflow import Workflow
from app import datasources
from app.datasources import DataSourceRecord, DataSourceRepository


def query_model_calls(model):
    """Planning/repair assertions exclude the optional post-execution narrator."""
    from semora_agent.sql_prompts import RESULT_SUMMARY

    return [call for call in model.complete_json.await_args_list if call.args[0] != RESULT_SUMMARY]


def harness(monkeypatch, *, validation_error=None):
    monkeypatch.delenv('METADATA_DATABASE_URL', raising=False)
    ont = {'domain': 'test', 'version': '1', 'dataSourceId': 'source',
           'knowledgeBaseIds': [], 'objects': [
               {'name': 'item', 'table': 'items', 'primaryKey': 'id', 'properties': [
                   {'name': 'id', 'column': 'id', 'type': 'integer', 'sensitive': True},
                   {'name': 'amount', 'column': 'amount', 'type': 'decimal'}]}],
           'relations': [], 'metrics': []}
    repo = DataSourceRepository()
    repo.save(DataSourceRecord('source', 'Test source', 'postgresql', 'DATASOURCE_TEST_URL', status='healthy'))
    monkeypatch.setattr(datasources.datasource_service, 'repository', repo)
    async def snapshot(_):
        return OntologySnapshotRef(domain='test', version='1', content_hash='hash', artifact_version='1', data_source_id='source'), copy.deepcopy(ont)
    store = Store()
    session = store.create_session('test', domain='test')
    database=sqlite3.connect(':memory:')
    database.executescript("CREATE TABLE items(id INTEGER PRIMARY KEY, amount NUMERIC,flag INTEGER,kind TEXT); INSERT INTO items VALUES(1,5,1,'x'),(2,20,0,'y'); CREATE TABLE other_items(id INTEGER PRIMARY KEY); INSERT INTO other_items VALUES(1);")
    async def query(sql,parameters,sensitive):
        assert sensitive==[]
        sql=re.sub(r'%\(([A-Za-z_][A-Za-z0-9_]*)\)s',r':\1',sql)
        cursor=database.execute(sqlglot.transpile(sql,read='postgres',write='sqlite')[0],parameters)
        rows=[list(row) for row in cursor.fetchall()]
        return ExecutionResult(columns=[c[0] for c in cursor.description],rows=rows,column_types=['BIGINT']*len(cursor.description),row_count=len(rows),duration_ms=1)
    execute = AsyncMock(side_effect=query)
    estimate = AsyncMock(return_value={'estimated_rows': 100}, side_effect=validation_error)
    w = Workflow(store, execute=execute, snapshot_loader=snapshot, estimate=estimate)
    return w, session, execute, estimate


class NativeModel:
    configured = required = True

    def __init__(self, calls):
        self.calls = iter(calls)
        self.messages = []

    async def complete_messages(self, messages, tools, **kwargs):
        self.messages.append(messages)
        name, args = next(self.calls)
        assert name in {t['function']['name'] for t in tools}
        return {'content': 'This text must not be persisted.', 'reasoning_content': 'PRIVATE_REASONING', 'tool_calls': [
            {'id': f'call-{len(self.messages)}', 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(args)}}
        ]}

    async def complete_json(self, *args):
        raise AssertionError('The Agent must use native tool calls, not one-shot JSON')
