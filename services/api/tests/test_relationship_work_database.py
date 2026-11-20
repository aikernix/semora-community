"""Opt-in tests for a disposable PostgreSQL database only."""
import asyncio
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace
from uuid import uuid4
import psycopg
import pytest
from app.relationship_keywords import KeywordDiscovery
from app.relationship_work import RelationshipWork


def test_concurrent_keyword_commits_and_database_stage_markers(monkeypatch):
    dsn=os.getenv('RELATIONSHIP_TEST_DATABASE_URL')
    if not dsn:pytest.skip('requires disposable PostgreSQL')
    connect=lambda:psycopg.connect(dsn,autocommit=True)
    with connect() as c:
        c.execute('CREATE SCHEMA IF NOT EXISTS semora_meta')
        c.execute('CREATE TABLE IF NOT EXISTS semora_meta.datasource_onboarding_jobs(id varchar(64) PRIMARY KEY,status text,completed_at timestamptz)')
        migration=Path(__file__).resolve().parents[3]/'deploy/postgres/migrations/016-relationship-keyword-work.sql'
        c.execute(migration.read_text().replace(':"api_role"','postgres'))
        job='test_'+uuid4().hex
        c.execute("INSERT INTO semora_meta.datasource_onboarding_jobs(id,status) VALUES(%s,'running')",(job,))
    repo=SimpleNamespace(dsn=dsn,_connect=connect)
    cards={str(i):{'table_id':str(i),'datasource_id':'source','physical_table':str(i),
        'name':'entity_'+str(i),'comment':'','columns':[{'name':'entity_ref','type':'bigint','comment':''}]} for i in range(2)}
    engine=KeywordDiscovery(cards,None,{'job_id':job},lambda:None,repository=repo)
    barrier=Barrier(2)
    def commit(index):
        work=RelationshipWork(repo,job,engine.work.input_hash,{},lambda:None)
        assert work.claim('keywords',str(index));barrier.wait(timeout=5)
        work.finish('keywords',str(index),{'keywords':['entity_ref','entity_0']},['entity_ref','entity_0'])
    with ThreadPoolExecutor(max_workers=2) as pool:list(pool.map(commit,range(2)))
    assert engine.work.keywords()==['entity_0','entity_ref']
    engine.finish_stage('keywords',{'keyword_count':2})
    # A new process has no file flags, but must honor the committed database marker.
    restored=KeywordDiscovery(cards,None,{'job_id':job},lambda:None,repository=repo)
    assert not restored.scope
    matches=asyncio.run(restored.retrieve_all())
    assert [r['total'] for r in matches]==[1,2]
    assert restored.work.keywords()==['entity_ref']
    again=KeywordDiscovery(cards,None,{'job_id':job},lambda:None,repository=repo)
    assert again.stage_result('retrieval')['counts']=={'entity_0':1,'entity_ref':2}
    assert again.work.keywords()==['entity_ref']
    assert asyncio.run(again.retrieve_all())==matches

    class Model:
        calls=0
        async def complete_json(self,system,user):
            import json
            self.calls+=1;p=json.loads(user)
            return {'groups':[{'table_ids':g['table_ids'],
                               'reason':'编号用途一致，保留'} for g in p['groups']], 'excluded':[]}
    monkeypatch.setattr('app.relationship_organization.ORGANIZATION_GROUP_LIMIT',1)
    model=Model();again.model=model
    organized=asyncio.run(again.organize())
    assert model.calls==1
    from_database=KeywordDiscovery(cards,None,{'job_id':job},lambda:None,repository=repo)
    assert asyncio.run(from_database.organize())==organized
    assert from_database.stage_result('organization')['metrics']['output_groups']==1
