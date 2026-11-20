from copy import deepcopy

from semora_contracts import SQLProgram
from app.sql_execution_scope import execution_scope, program_output_contract
from app.sql_model_context import sql_model_payload
from app.query_context_wire import decode_material, FORMAT
from test_sql_model_context import material


def payload():
    return {'materials':material(), 'data_sources':[{'id':'s1','dialect':'mysql','version':'5.7'}],
            'request':{'summary':'Read the latest records and supplement matching information'},
            'original_request':'Read the latest records and supplement matching information'}


def unpack(value):
    p=value['materials']
    return decode_material(p) if p.get('format')==FORMAT else p


def test_same_source_catalog_and_default_plan_are_available_before_generation():
    p=payload();before=deepcopy(p)
    view=sql_model_payload(p)
    assert view['execution_scope']['preferred_plan']=='single_source_statement'
    assert view['execution_scope']['source_targets']==[
        {'engine':'source','data_source_id':'s1','dialect':'mysql','tables':['app.details','app.records']}]
    assert p==before


def test_output_contract_separates_concrete_source_targets_and_local_results():
    p=payload()
    original=SQLProgram.model_json_schema();before=deepcopy(original)
    contract=program_output_contract(p,original)
    assert original==before
    source=contract['$defs']['SourceSQLTarget0']['allOf'][1]['properties']
    local=contract['$defs']['LocalResultSQL']['allOf'][1]['properties']
    assert source['engine']['const']=='source' and source['data_source_id']['const']=='s1'
    assert source['dialect']['const']=='mysql' and 'execution_scope' in source['sql']['description']
    assert local['engine']['const']=='local' and local['data_source_id']['const']==''
    assert local['dialect']['const']=='duckdb' and local['inputs']['maxItems']==0
    assert contract['properties']['steps']['prefixItems'][0]['oneOf']==[{'$ref':'#/$defs/SourceSQLTarget0'}]


def test_cross_source_same_table_names_keep_separate_namespaces():
    p=payload()
    p['materials']['objects'][1]['object'].update(dataSourceId='s2',table='app.records')
    p['data_sources'].append({'id':'s2','dialect':'postgres'})
    scope=execution_scope(p)
    assert scope['preferred_plan']=='source_queries_then_result_merge'
    assert [t['tables'] for t in scope['source_targets']]==[['app.records'],['app.records']]
    contract=program_output_contract(p,SQLProgram.model_json_schema())
    assert contract['$defs']['SourceSQLTarget1']['allOf'][1]['properties']['data_source_id']['const']=='s2'


def test_local_repair_gets_result_columns_without_physical_catalog_or_prior_source_sql():
    p=payload()
    p['request']['logical_plan']={'output_refs':['obj_a.key_internal']}
    p.update(current_step='merged',program={'summary':'Merge results','steps':[
        {'name':'roots','engine':'source','data_source_id':'s1','sql':'SELECT KEY FROM app.records',
         'output_columns':['entity_key'],'purpose':'Read matching records'},
        {'name':'merged','engine':'local','sql':'SELECT entity_key FROM roots','output_columns':['entity_key']}]})
    before=deepcopy(p)
    view=sql_model_payload(p)
    assert view['execution_scope']['current_target']['result_tables']=={'roots':['entity_key']}
    assert unpack(view)=={'result_tables':[{'table':'roots','columns':['entity_key']}]}
    assert view['data_sources']==[] and 'logical_plan' not in view['request']
    assert 'sql' not in view['program']['steps'][0]
    assert view['program']['steps'][1]['sql']=='SELECT entity_key FROM roots'
    assert p==before


def test_source_repair_keeps_only_current_source_definitions():
    p=payload()
    p['materials']['objects'][1]['object']['dataSourceId']='s2'
    p['data_sources'].append({'id':'s2','dialect':'postgres'})
    p.update(current_step='read',program={'steps':[{'name':'read','engine':'source','data_source_id':'s1','output_columns':['KEY']}]})
    view=sql_model_payload(p);packet=unpack(view)
    assert [s['id'] for s in view['data_sources']]==['s1']
    assert [o['object']['table'] for o in packet['objects']]==['app.records']
    assert packet['relations']==[]
    assert view['execution_scope']['current_target']['tables']==['app.records']


def test_source_repair_does_not_mix_same_named_tables_across_databases():
    p=payload()
    p['materials']['objects'][1]['object'].update(dataSourceId='s2',table='app.records')
    p['materials']['schemas'].append({'object':'obj_b','tables':[{'name':'records','columns':[
        {'name':'KEY','type':'integer'}]}]})
    p['data_sources'].append({'id':'s2','dialect':'postgres'})
    p.update(current_step='read',program={'steps':[{'name':'read','engine':'source','data_source_id':'s1','output_columns':['KEY']}]})
    packet=unpack(sql_model_payload(p))
    assert len(packet['schemas'])==1 and packet['schemas'][0]['dataSourceId']=='s1'
    assert packet['relations']==[]


def test_legacy_source_without_dialect_keeps_its_existing_empty_dialect_contract():
    p=payload();p['data_sources'][0].pop('dialect')
    assert sql_model_payload(p)['execution_scope']['source_targets'][0]['dialect']==''


def test_result_only_calculation_and_legitimate_single_source_local_processing_remain_available():
    empty={'materials':{'objects':[]},'data_sources':[]}
    contract=program_output_contract(empty,SQLProgram.model_json_schema())
    assert 'prefixItems' not in contract['properties']['steps']
    assert contract['properties']['steps']['items']['oneOf']==[{'$ref':'#/$defs/LocalResultSQL'}]
    p=payload()
    p['program_steps']=[{'name':'raw','engine':'source','data_source_id':'s1','output_columns':['amount']},
                        {'name':'total','engine':'local','output_columns':['total']}]
    assert execution_scope(p)['steps'][1]['result_tables']=={'raw':['amount']}
