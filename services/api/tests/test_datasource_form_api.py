from fastapi.testclient import TestClient

from app import database_routes, main
from app.datasource_modeling import ModelingRepository, ModelingService, package_objects
from app.datasources import DataSourceRecord, DataSourceRepository, DataSourceService


def _table(identifier, name, columns, primary=None):
    return {
        "id": identifier,
        "schema": "public",
        "name": name,
        "kind": "TABLE",
        "description": f"{name} table",
        "columns": [
            {"name": column, "type": kind, "raw_type": kind, "nullable": False}
            for column, kind in columns
        ],
        "primary_keys": [{"name": "pk", "columns": primary or []}] if primary else [],
        "unique_constraints": [],
        "foreign_keys": [],
        "indexes": [],
    }


def test_table_catalog_is_paginated_from_persisted_snapshot():
    repository = DataSourceRepository(dsn="")
    source = DataSourceRecord("source", "Source", "postgresql", "DATASOURCE_SOURCE_URL")
    source.scan_result["tables"] = [
        _table(f"public.table_{index}", f"table_{index}", [("id", "bigint")], ["id"])
        for index in range(35)
    ]
    repository.save(source)
    page = repository.table_page("source", "table_2", page=1, page_size=5)
    assert page["total"] == 11
    assert len(page["items"]) == 5
    assert repository.table_detail("source", page["items"][0]["id"])["columns"][0]["name"] == "id"


def test_relation_review_disable_enable_and_manual_edit(monkeypatch):
    tables = [
        _table("public.entity", "entity", [("id", "bigint")], ["id"]),
        _table("public.event", "event", [("id", "bigint"), ("entity_id", "bigint")], ["id"]),
    ]
    source_repository = DataSourceRepository(dsn="")
    record = DataSourceRecord(
        "source", "Source", "postgresql", "DATASOURCE_SOURCE_URL", status="healthy", objects=2
    )
    record.scan_result["tables"] = tables
    source_repository.save(record)
    source_service = DataSourceService(repository=source_repository)
    modeling_repository = ModelingRepository(dsn="")
    modeling = ModelingService(source_service, executor_request=None, repository=modeling_repository)
    objects, _ = package_objects("source", tables)
    from app.datasource_table_storage import tables_digest

    schema_hash = tables_digest(tables)
    modeling_repository.save_package(
        "source",
        schema_hash,
        {"objects": objects, "relations": []},
        "ready_with_pending",
        1,
    )
    candidate = {
        "id": "candidate",
        "scope": "single_source",
        "source_datasource_id": "source",
        "target_datasource_id": "source",
        "source_table_id": "public.event",
        "target_table_id": "public.entity",
        "source_columns": ["entity_id"],
        "target_columns": ["id"],
        "key_pairs": [{"source": "entity_id", "target": "id"}],
        "condition_template": {"operator": "and_equals"},
        "evidence": [],
        "evidence_grade": "medium",
        "confidence": 0.8,
        "cardinality": "N:1",
        "status": "pending",
        "domain": None,
    }
    modeling_repository.replace_candidates([candidate], schema_hash, schema_hash)
    monkeypatch.setattr(database_routes, "datasource_service", source_service)
    monkeypatch.setattr(database_routes, "modeling_service", modeling)
    client = TestClient(main.app)

    initial=client.get("/api/v1/datasources/source/relationships").json()
    assert initial["total"] == 1 and initial["counts"] == {"accepted":0,"pending":1,"disabled":0,"all":1}
    assert client.get("/api/v1/datasources/source/relationships?status=pending").json()["total"] == 1
    from copy import deepcopy
    class Ontology:
        def __init__(self):
            self.configs={'bound':{'domain':'bound','sourcePackageRefs':[{'dataSourceId':'source'}],'relations':[{'name':'keep_cross','from':'x','to':'y'}]},
                          'unrelated':{'domain':'unrelated','sourcePackageRefs':[],'relations':[]}}
            self.published=[]
            self.packages={}
        async def update_source_package(self,value):
            self.packages[value['dataSourceId']]=deepcopy(value['payload'])
            self.published.extend(k for k,v in self.configs.items() if any(r['dataSourceId']==value['dataSourceId'] for r in v['sourcePackageRefs']))
        async def list_drafts(self):return [{'domain':k} for k in self.configs]
        async def get_configuration(self,domain):return deepcopy(self.configs[domain])
        async def save_configuration(self,domain,value):
            self.configs[domain]=deepcopy(value);self.published.append(domain);return value
    ontology=Ontology()
    modeling.ontology=ontology
    monkeypatch.setattr(database_routes,'ontology_service',ontology)
    # Manual mappings also use original types without a family whitelist.
    record.scan_result['tables'][1]['columns'][1]['type']='VENDOR_IDENTIFIER'
    record.scan_result['tables'][1]['columns'][1]['raw_type']='VENDOR_IDENTIFIER'
    source_repository.save(record)
    modeling_repository.memory_packages['source']['schema_hash']=tables_digest(record.scan_result['tables'])
    schema_hash=tables_digest(record.scan_result['tables'])
    modeling_repository.replace_candidates([candidate],schema_hash,schema_hash)
    body={'source_table_id':'public.event','target_table_id':'public.entity','key_pairs':[{'source':'entity_id','target':'id'}],'cardinality':'N:1'}
    created=client.post('/api/v1/datasources/source/relationships',json=body)
    assert created.status_code==201,created.text
    identifier=created.json()['id']
    assert 'confidence' not in created.json()
    assert len(ontology.configs['bound']['relations'])==1
    assert len(ontology.packages['source']['relations'])==1
    assert ontology.published==['bound']
    edited=client.put('/api/v1/datasources/source/relationships/'+identifier,json={**body,'cardinality':'1:1'})
    assert edited.status_code==200,edited.text
    assert ontology.packages['source']['relations'][-1]['cardinality']=='1:1'
    disabled=client.delete('/api/v1/datasources/source/relationships/'+identifier)
    assert disabled.status_code==200 and disabled.json()['deleted'] is False and disabled.json()['status']=='disabled'
    assert ontology.configs['bound']['relations']==[{'name':'keep_cross','from':'x','to':'y'}]
    assert ontology.configs['unrelated']['relations']==[]
    assert modeling_repository.get_package('source')['payload']['relationshipRecords']==[]
    assert client.get('/api/v1/datasources/source/relationships').json()['counts']=={'accepted':0,'pending':1,'disabled':1,'all':2}
    restored=client.post('/api/v1/datasources/source/relationships/'+identifier+'/restore')
    assert restored.status_code==200 and restored.json()['status']=='accepted'
    approved=client.post('/api/v1/datasources/source/relationships/candidate/decision',json={'action':'approve'})
    assert approved.status_code==200 and approved.json()['status']=='accepted'
    assert len(modeling_repository.get_package('source')['payload']['relationshipRecords'])==2
    rejected=client.post('/api/v1/datasources/source/relationships/candidate/decision',json={'action':'reject'})
    assert rejected.status_code==409
    stopped=client.post('/api/v1/datasources/source/relationships/candidate/decision',json={'action':'disable'})
    assert stopped.status_code==200 and stopped.json()['status']=='disabled'
    assert len(modeling_repository.get_package('source')['payload']['relationshipRecords'])==1
    assert client.get('/api/v1/datasources/source/relationships?status=disabled').json()['total']==1
    enabled=client.post('/api/v1/datasources/source/relationships/candidate/decision',json={'action':'enable'})
    assert enabled.status_code==200 and enabled.json()['status']=='accepted'
    second={**candidate,'id':'candidate-2','status':'unresolved','research_pending':True}
    modeling_repository.replace_candidates([second],schema_hash,schema_hash)
    refused=client.post('/api/v1/datasources/source/relationships/candidate-2/decision',json={'action':'reject'})
    assert refused.status_code==200 and refused.json()['status']=='disabled'
    assert modeling_repository.memory_candidates['candidate-2']['reviewed_by']=='data-steward'
    pending={**candidate,'id':'editable-pending','status':'unresolved','research_pending':True}
    modeling_repository.replace_candidates([pending],schema_hash,schema_hash)
    before=len(modeling_repository.get_package('source')['payload']['relations'])
    edited_pending=client.put('/api/v1/datasources/source/relationships/editable-pending',json=body)
    assert edited_pending.status_code==200,edited_pending.text
    assert edited_pending.json()['status']=='accepted'
    assert modeling_repository.memory_candidates['editable-pending']['evidence_grade']=='manual'
    assert modeling_repository.memory_candidates['editable-pending']['research_pending'] is False
    assert len(modeling_repository.get_package('source')['payload']['relations'])==before+1
    unknown={**candidate,'id':'unknown-pending','status':'pending','cardinality':'unknown'}
    modeling_repository.replace_candidates([unknown],schema_hash,schema_hash)
    writes=len(ontology.published)
    blocked=client.post('/api/v1/datasources/source/relationships/unknown-pending/decision',json={'action':'approve'})
    assert blocked.status_code==409
    assert modeling_repository.memory_candidates['unknown-pending']['status']=='pending'
    assert len(ontology.published)==writes
    assert client.put('/api/v1/datasources/source/relationships/unknown-pending',json={**body,'cardinality':'unknown'}).status_code==422
    rejected_unknown=client.post('/api/v1/datasources/source/relationships/unknown-pending/decision',json={'action':'reject'})
    assert rejected_unknown.status_code==200 and rejected_unknown.json()['status']=='disabled'
    assert client.post('/api/v1/datasources/source/relationships/unknown-pending/decision',json={'action':'enable'}).status_code==409
    fixed=client.put('/api/v1/datasources/source/relationships/unknown-pending',json={**body,'cardinality':'N:1'})
    assert fixed.status_code==200 and fixed.json()['status']=='accepted'


def test_successful_candidate_replacement_prunes_only_stale_automatic_records():
    repository=ModelingRepository(dsn='')
    base={'scope':'single_source','source_datasource_id':'source','target_datasource_id':'source',
          'source_table_id':'public.event','target_table_id':'public.entity','source_columns':['entity_id'],
          'target_columns':['id'],'key_pairs':[{'source':'entity_id','target':'id'}],
          'condition_template':{'operator':'and_equals'},'evidence':[],'evidence_grade':'medium',
          'cardinality':'N:1','domain':None}
    stale={**base,'id':'stale','status':'unresolved'}
    manual={**base,'id':'manual','status':'accepted','reviewed_by':'data-steward'}
    disabled={**base,'id':'disabled','status':'disabled','reviewed_by':'data-steward'}
    current={**base,'id':'current','status':'accepted'}
    repository.replace_candidates([stale,manual,disabled],'hash','hash')
    repository.replace_candidates([current],'hash','hash',prune_source_id='source')
    assert set(repository.memory_candidates)=={'manual','disabled','current'}
    repository.replace_candidates([],'hash','hash',prune_source_id='source')
    assert set(repository.memory_candidates)=={'manual','disabled'}
