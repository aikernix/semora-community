from copy import deepcopy

from app.sql_input_compaction import compact_sql_input,compact_output_schema


def source(name):
    return {'id':name,'name':name,'type':'mysql','vendor':'MySQL','dialect':'mysql','version':'5.7',
        'features':{'common_table_expressions':False,'window_functions':None},
        'feature_status':{'common_table_expressions':'unsupported'},'driver_version':'driver',
        'capabilities':{'connection':'verified','cte':{'status':'unsupported'},'special_join':{'status':'unsupported'}},
        'sql_baseline':{'pagination':'rownum'},
        'function_capabilities':{'fn.sum.integer':{'status':'verified','scope':'synthetic_signature','method':'cache'},
            'fn.custom':{'status':'unsupported','scope':'observed_expression','expression':'CUSTOM(a)'}},
        'sql_feedback':[{'reason':'Preserve exact restrictions'}]}


def test_sql_projection_preserves_user_facts_structure_and_capability_scope():
    packet={'objects':[{'object':{'table':'items','properties':[{'column':'title','label':'Title','nullable':True}]}}],
            'relations':[{'condition':'a.id=b.id AND a.kind=b.kind'}],
            'knowledge':[],'knowledge_retrieval':{'diagnostics':'unused'}}
    p={'original_request':'Match by both identifiers',
       'confirmed_answers':[{'value':'and','label':'Both must match'}],
       'request':{'summary':'Match by both identifiers','sequence':['Read then match'],
                  'analysis_steps':['Display-only older summary'],'version':3,
                  'logical_plan':{'sequence':['Incomplete older summary'],
                    'join_filters':[{'field_refs':['a.id','b.id'],'operator':'eq'},
                                    {'field_refs':['a.kind','b.kind'],'operator':'eq'}]}},
       'materials':packet,'data_sources':[source('a'),source('b')]}
    p['data_sources'][1]['features']['window_functions']=False
    before=deepcopy(p);result=compact_sql_input(p)
    assert p==before
    assert result['original_request']==p['original_request']
    assert result['confirmed_answers']==p['confirmed_answers']
    assert result['materials']['objects']==packet['objects']
    assert result['materials']['relations']==packet['relations']
    assert result['request']['sequence']==['Read then match']
    assert 'sequence' not in result['request']['logical_plan']
    assert len(result['request']['logical_plan']['join_filters'])==2
    assert 'analysis_steps' not in result['request']
    assert len(result['function_profiles'])==1
    for index,s in enumerate(result['data_sources']):
        assert s['features']==before['data_sources'][index]['features']
        assert s['sql_restrictions']=={'special_join':{'status':'unsupported'}}
        assert s['pagination_strategy']=='rownum'
        functions=result['function_profiles'][s['function_profile_ref']]
        assert functions['fn.custom']['expression']=='CUSTOM(a)' and functions['fn.custom']['status']=='unsupported'
        assert functions['fn.sum.integer']['scope']=='synthetic_signature'
        assert s['sql_feedback']==before['data_sources'][0]['sql_feedback']


def test_different_capability_scopes_or_statuses_never_share():
    a,b=source('a'),source('b');b['function_capabilities']['fn.sum.integer']['scope']='observed_expression'
    result=compact_sql_input({'data_sources':[a,b]})
    assert 'function_profiles' not in result
    assert result['data_sources'][0]['function_capabilities']['fn.sum.integer']['scope']=='synthetic_signature'


def test_real_knowledge_and_fallback_processing_order_remain():
    p={'request':{'analysis_steps':['Only available order']},'materials':{'knowledge':[{'title':'Document','content':'Use full history'}]}}
    out=compact_sql_input(p)
    assert out['materials']==p['materials'] and out['request']['sequence']==['Only available order']


def test_schema_titles_are_removed_without_deleting_title_properties_or_literals():
    schema={'title':'Envelope','properties':{'title':{'title':'Business title','type':'string','minLength':1}},
            'required':['title'],'additionalProperties':False,'default':{'title':'Literal'},
            '$defs':{'title':{'title':'Definition','const':{'title':'Data'}}}}
    out=compact_output_schema(schema)
    assert 'title' not in out and out['properties']['title']=={'type':'string','minLength':1}
    assert out['required']==['title'] and out['additionalProperties'] is False
    assert out['default']=={'title':'Literal'} and out['$defs']['title']['const']=={'title':'Data'}


def test_complete_join_comparisons_translate_and_validate_without_losing_second_key():
    from semora_contracts import LogicalQueryPlan
    from app.query_alignment import validate_logical_refs
    from app.sql_model_context import sql_model_payload
    from test_sql_model_context import material
    raw=material()
    plan=LogicalQueryPlan(join_filters=[
        {'field_refs':['obj_a.key_internal','obj_b.key_internal'],'operator':'eq','description':'Match identifiers'},
        {'field_refs':['obj_a.kind_internal','obj_b.kind_internal'],'operator':'null_safe_eq','description':'Match category including missing category'},
    ])
    assert validate_logical_refs(plan,raw)==[]
    view=sql_model_payload({'materials':raw,'request':{'logical_plan':plan.model_dump()}})
    comparisons=view['request']['logical_plan']['join_filters']
    assert len(comparisons)==2 and comparisons[1]['operator']=='null_safe_eq'
    assert comparisons[1]['field_refs']==[
        {'table':'app.records','column':'KIND','dataSourceId':'s1'},
        {'table':'app.details','column':'KIND','dataSourceId':'s1'}]
    plan.join_filters[1].field_refs[1]='obj_b.missing'
    assert validate_logical_refs(plan,raw)==['未知属性引用：obj_b.missing']


def test_declared_metric_output_does_not_invalidate_a_complete_logical_plan():
    from semora_contracts import LogicalQueryPlan
    from app.query_alignment import validate_logical_refs
    from test_sql_model_context import material
    plan=LogicalQueryPlan(output_refs=['total'],metrics=[
        {'id':'total','label':'Total records','operation':'count','field_refs':['obj_a.key_internal']}])
    assert validate_logical_refs(plan,material())==[]
    plan.output_refs.append('undeclared')
    assert validate_logical_refs(plan,material())==['未知属性引用：undeclared']
    plan.metrics[0].field_refs=['obj_a.missing']
    assert '未知属性引用：obj_a.missing' in validate_logical_refs(plan,material())
