from copy import deepcopy
import json

import pytest
import sqlglot
from sqlglot import exp

from app.sql_model_context import sql_model_payload
from app.query_context_wire import FORMAT, decode_material


def material():
    return {'objects':[{'object':{'name':n,'table':t,'dataSourceId':s,
        'primaryKey':'key_internal','properties':[
            {'name':'key_internal','column':'KEY','type':'string','comment':'编号'},
            {'name':'kind_internal','column':'KIND','enumValues':['obj_a.key_internal']},
        ]}} for n,t,s in [('obj_a','app.records','s1'),('obj_b','app.details','s1')]],
        'schemas':[{'object':'obj_a','tables':[{'name':'records','columns':[
            {'name':'KEY','type':'varchar(20)'},{'name':'KIND','type':'varchar(20)'}]}]}],
        'relations':[{'name':'relation_internal','from':'obj_a','to':'obj_b',
            'condition':"obj_a.key_internal = obj_b.key_internal AND obj_b.kind_internal = 'obj_a.key_internal'",'cardinality':'1:N'}],
        'identity_catalog':[{'object':'obj_unselected','primaryKey':'internal'}],
        'selected_objects':['obj_a','obj_b']}


def unpack(payload):
    value=payload['materials']
    return decode_material(value) if value.get('format')==FORMAT else value


def test_sql_writers_get_only_physical_mappings_without_mutating_semantic_snapshot():
    raw=material();before=deepcopy(raw)
    payload={'materials':raw,'request':{'summary':'按原需求查询','logical_plan':{
        'subject_refs':['obj_a'],'grain_refs':['obj_a.key_internal'],
        'relation_refs':['relation_internal']}},'original_request':'保留用户原话'}
    result=sql_model_payload(payload);data=unpack(result)
    assert raw==before and payload['request']['logical_plan']['subject_refs']==['obj_a']
    obj=data['objects'][0]['object']
    assert 'name' not in obj and obj['table']=='app.records'
    assert obj['primaryKey']=='KEY' and obj['properties'][0]['column']=='KEY'
    assert 'name' not in obj['properties'][0]
    # Values that resemble references remain business data, including SQL literals.
    assert obj['properties'][1]['enumValues']==['obj_a.key_internal']
    condition=sqlglot.parse_one(data['relations'][0]['condition'])
    assert [s.this for s in condition.find_all(exp.Literal)]==['obj_a.key_internal']
    assert {c.name for c in condition.find_all(exp.Column)}=={'KEY','KIND'}
    assert 'name' not in data['relations'][0]
    assert data['relations'][0]['from']=='app.records'
    assert data['relations'][0]['fromDataSourceId']=='s1'
    assert data['schemas'][0]['object']=='app.records'
    assert 'identity_catalog' not in data and 'selected_objects' not in data
    logical=result['request']['logical_plan']
    assert logical['grain_refs']==[{'dataSourceId':'s1','table':'app.records','column':'KEY'}]
    assert 'relation_internal' not in json.dumps(logical)
    assert result['original_request']=='保留用户原话'


def test_same_physical_table_names_across_sources_keep_distinct_fields_and_scans():
    raw=material()
    raw['objects'][1]['object']['table']='app.records'
    raw['objects'][1]['object']['dataSourceId']='s2'
    raw['schemas'].append({'object':'obj_b','tables':[{'name':'records','columns':[
        {'name':'KEY','type':'integer'}]}]})
    result=sql_model_payload({'materials':raw,'logical_plan':{
        'output_refs':['obj_a.key_internal','obj_b.key_internal']}})
    data=unpack(result)
    assert [r['dataSourceId'] for r in result['logical_plan']['output_refs']]==['s1','s2']
    assert data['schemas'][0]['tables'][0]['columns'][0]['type']=='varchar(20)'
    assert data['schemas'][1]['tables'][0]['columns'][0]['type']=='integer'
    assert data['relations'][0]['toDataSourceId']=='s2'


def test_unknown_expression_mapping_is_not_guessed():
    raw=material();raw['relations'][0]['condition']='obj_a.missing = obj_b.key_internal'
    with pytest.raises(ValueError,match='不存在的属性'):
        sql_model_payload({'materials':raw})


def test_projection_alias_and_computed_expression_use_physical_columns():
    raw=material()
    obj=raw['objects'][0]['object']
    obj['projection']='SELECT a.key_internal AS visible_key FROM obj_a a'
    obj['properties'][1]['expression']='key_internal'
    data=unpack(sql_model_payload({'materials':raw}))
    projected=data['objects'][0]['object']
    tree=sqlglot.parse_one(projected['projection'])
    assert next(tree.find_all(exp.Table)).name=='records'
    assert next(tree.find_all(exp.Column)).name=='KEY'
    assert next(tree.find_all(exp.Column)).table=='a'
    assert projected['properties'][1]['expression']=='"KEY"'
