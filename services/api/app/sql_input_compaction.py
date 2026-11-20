"""SQL model projections only; keep authoritative requests and metadata intact."""
from copy import deepcopy
import json


def compact_output_schema(schema):
    """Remove display titles, never property names, literal values or guards."""
    result=deepcopy(schema)
    result.pop('title',None)
    maps={'properties','$defs','definitions','patternProperties','dependentSchemas'}
    children={'items','additionalProperties','unevaluatedProperties','contains','not','if','then','else','propertyNames'}
    for key,value in list(result.items()):
        if key in maps and isinstance(value,dict):
            result[key]={name:compact_output_schema(item) for name,item in value.items()}
        elif key in children and isinstance(value,dict):
            result[key]=compact_output_schema(value)
        elif key in {'oneOf','anyOf','allOf','prefixItems'} and isinstance(value,list):
            result[key]=[compact_output_schema(item) for item in value]
    return result


def compact_sql_input(payload):
    result=deepcopy(payload)
    function_sets={}
    for source in result.get('data_sources',[]):
        # Scope, signatures, actual expressions and negative evidence remain.
        # Cache provenance and repeated status representations stay in storage.
        for key in ('driver_version','feature_status'):
            source.pop(key,None)
        if str(source.get('type','')).casefold()==str(source.get('vendor','')).casefold():source.pop('type',None)
        capabilities=source.pop('capabilities',{})
        baseline=source.pop('sql_baseline',{})
        pagination=capabilities.get('pagination_strategy') or baseline.get('pagination')
        if pagination:source['pagination_strategy']=pagination
        # Keep extensions unknown to this projection instead of guessing that
        # future vendor-specific SQL restrictions are irrelevant.
        known={'mdl','query','comments','structure','connection','constraints','live_validation',
               'cte','window_functions','recursive_cte','offset_pagination','types','pagination','pagination_strategy'}
        extra={k:v for k,v in capabilities.items() if k not in known}
        from semora_contracts.database import SQL_OPTIONAL_FEATURES
        for key,feature in SQL_OPTIONAL_FEATURES.items():
            if key in capabilities and feature not in source.get('features',{}):extra[key]=capabilities[key]
        for key,value in capabilities.items():
            if key in {'types','pagination'} and isinstance(value,dict) and value.get('status') in {'unsupported','mixed'}:
                extra[key]=value
        if extra:source['sql_restrictions']=extra
        for value in source.get('function_capabilities',{}).values():
            if isinstance(value,dict):
                for key in ('method','observed_at','expires_at'):value.pop(key,None)
        functions=source.get('function_capabilities')
        if functions:
            signature=json.dumps(functions,ensure_ascii=False,sort_keys=True,separators=(',',':'))
            function_sets.setdefault(signature,[]).append(source)
    shared={}
    for signature,sources in function_sets.items():
        if len(sources)<2:continue
        name='functions_'+str(len(shared)+1)
        shared[name]=sources[0]['function_capabilities']
        for source in sources:
            source.pop('function_capabilities')
            source['function_profile_ref']=name
    if shared:
        result['function_profiles']=shared
        result['function_profile_guide']='function_profile_ref引用function_profiles中的完整函数能力；各数据源的features仍独立生效。'
    packet=result.get('materials',{})
    if isinstance(packet,dict) and not packet.get('knowledge'):
        packet.pop('knowledge_retrieval',None)
        packet.pop('knowledge_keywords',None)
    for key in ('request','confirmed_requirement'):
        request=result.get(key)
        if not isinstance(request,dict):continue
        plan=request.get('logical_plan')
        # sequence is the one business processing order. Presentation and old
        # plan summaries are generated interpretations, not user confirmations.
        if not request.get('sequence'):
            request['sequence']=request.get('analysis_steps') or (plan or {}).get('sequence',[])
        request.pop('analysis_steps',None)
        request.pop('version',None)
        if isinstance(plan,dict):plan.pop('sequence',None)
    logical=result.get('logical_plan')
    if isinstance(logical,dict):logical.pop('sequence',None)
    return result
