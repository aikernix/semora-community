import json
from copy import deepcopy
from app.query_context_wire import encode_material,decode_material


def packet():
    props=[{'name':f'field_{i}','column':f'COLUMN_{i}','type':'string',
        'label':'字段用途与编号含义说明'*5,'description':'字段用途与编号含义说明'*5,
        'nullable':i!=0,'sensitive':False} for i in range(100)]
    props[1]['enumValues']={'0':'停用','1':'启用'}
    props[2]['unit']='件'
    return {'objects':[{'object':{'name':'record','properties':props}}],
        'schemas':[{'object':'record','tables':[{'id':'schema.record','columns':[
            {'name':p['column'],'type':'varchar(50)','nullable':p['nullable'],'primary_key':i==0}
            for i,p in enumerate(props)],'primary_keys':[{'columns':['COLUMN_0']}]}]}],
        'relations':[{'from':'record','to':'target','condition':'record.COLUMN_1 = target.id AND target.kind = 1'}],
        'gaps':[]}


def test_wire_preserves_all_fields_enums_constraints_and_does_not_mutate_storage():
    raw=packet();before=deepcopy(raw);wire=encode_material(raw)
    assert raw==before
    restored=decode_material(json.loads(json.dumps(wire,ensure_ascii=False)))
    assert restored==raw
    assert len(json.dumps(wire,ensure_ascii=False).encode())<len(json.dumps(raw,ensure_ascii=False).encode())*.6


def test_sparse_rows_missing_null_and_nested_json_round_trip():
    raw={'objects':[], 'rows':[{'a':None,'b':[1,True]},{'a':1},{'x':{}},{'b':False}]}
    assert decode_material(encode_material(raw))==raw


def test_multiple_physical_tables_keep_original_scan_information():
    raw=packet();raw['schemas'][0]['tables'].append({'id':'another','columns':[{'name':'unknown','type':'numeric'}]})
    assert decode_material(encode_material(raw))==raw


def test_reserved_metadata_and_existing_scan_attributes_round_trip():
    raw=packet()
    raw['objects'][0]['object']['properties'][1]['enumValues']={'$columns':[], '$rows':[]}
    raw['objects'][0]['object']['properties'][2]['scan']={'$literal':{'a':1}}
    raw['objects'][0]['object']['properties'][3]['label_same_as_description']=True
    assert decode_material(encode_material(raw))==raw


def test_encoding_payload_keeps_internal_validation_material_unchanged():
    from app.query_context_wire import encode_payload
    raw={'materials':packet(), 'request':'统计数量'}
    before=deepcopy(raw)
    encoded=encode_payload(raw)
    assert raw==before
    assert decode_material(encoded['materials'])==raw['materials']
    assert encoded['request']==raw['request']


def test_tiny_material_does_not_grow():
    from app.query_context_wire import model_material
    raw={'objects':[], 'relations':[]}
    assert model_material(raw)==raw


def test_identical_definitions_are_shared_without_merging_table_identities():
    raw=packet()
    second=deepcopy(raw['objects'][0]);second['object'].update(name='second',dataSourceId='other')
    raw['objects'].append(second)
    # Keep matching native scan metadata on both objects.
    schema=deepcopy(raw['schemas'][0]);schema['object']='second';raw['schemas'].append(schema)
    before=deepcopy(raw);wire=encode_material(raw)
    assert raw==before and len(wire['shared_properties'])==1
    restored=decode_material(wire)
    assert restored==raw
    restored['objects'][0]['object']['properties'][0]['column']='changed'
    assert restored['objects'][1]['object']['properties'][0]['column']=='COLUMN_0'


def test_different_field_constraints_do_not_share_definitions():
    raw=packet();second=deepcopy(raw['objects'][0]);second['object']['name']='second'
    second['object']['properties'][0]['nullable']=True
    raw['objects'].append(second)
    assert 'shared_properties' not in encode_material(raw)


def test_previous_wire_version_still_decodes():
    raw=packet();wire=encode_material(raw);wire['format']='query-material-tables-v1'
    assert decode_material(wire)==raw


def test_supplement_tool_compacts_inner_packet_and_keeps_checkpoint_raw(monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from app.research_tools import ResearchTools
    import app.research_tools as research

    raw=packet()
    result={'available':True, 'prepared_context':raw, 'message':'资料已补齐'}
    monkeypatch.setattr(research, 'supplement_context', lambda *args: result)
    workflow=SimpleNamespace(emit=lambda *args:None)
    run=SimpleNamespace(events=[], continuation=None, status='analyzing', evidence=[])
    toolkit=ResearchTools(workflow, run, {}, None)
    toolkit.context_state={'packet':raw}
    tool=next(t for t in toolkit.tools() if t.name=='request_context')
    response=json.loads(asyncio.run(tool.ainvoke({'objects':['record'], 'purpose':'join', 'reason':'补齐关联字段'})))
    assert response['available'] is True
    assert decode_material(response['prepared_context'])==raw
    assert toolkit.context_state['packet']==raw
    assert 'format' not in result['prepared_context']
