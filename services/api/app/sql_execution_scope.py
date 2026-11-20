"""Give SQL writers explicit physical-source and result-table namespaces."""
from copy import deepcopy


def execution_scope(payload):
    objects = [o['object'] for o in payload.get('materials', {}).get('objects', [])]
    targets = []
    for source in sorted(payload.get('data_sources', []), key=lambda s: s['id']):
        tables = sorted({o['table'] for o in objects if o.get('dataSourceId') == source['id']})
        if tables:
            targets.append({'engine': 'source', 'data_source_id': source['id'],
                            'dialect': source.get('dialect', ''), 'tables': tables})
    scope = {'preferred_plan': 'single_source_statement' if len(targets) == 1 else 'source_queries_then_result_merge' if targets else 'result_computation',
             'source_targets': targets,
             'result_target': {'engine': 'local', 'dialect': 'duckdb',
                               'table_namespace': 'previous_steps.name', 'column_namespace': 'previous_steps.output_columns'}}
    steps = payload.get('program_steps') or (payload.get('program') or {}).get('steps', [])
    previous = []
    scopes = []
    for step in steps:
        if step.get('engine', 'source') == 'local':
            readable = {p['name']: p['output_columns'] for p in previous}
            current = {'name': step['name'], 'engine': 'local', 'dialect': 'duckdb', 'result_tables': readable}
        else:
            target = next((t for t in targets if t['data_source_id'] == step.get('data_source_id')), {})
            current = {'name': step['name'], **target}
        scopes.append(current)
        previous.append(step)
    if scopes:
        scope['steps'] = scopes
    return scope


def scoped_payload(payload):
    """Generation sees the catalog; a known repair target sees only its inputs."""
    result = deepcopy(payload)
    scope = execution_scope(result)
    result['execution_scope'] = scope
    current = next((s for s in scope.get('steps', []) if s['name'] == result.get('current_step')), None)
    if current is None:
        return result
    result['execution_scope'] = {'current_target': current}
    packet = result.get('materials', {})
    if current.get('engine') == 'local':
        # No physical catalog, relations or source SQL in the local writer's
        # input. Business requirements remain, so this is not table reselection.
        result['materials'] = {'result_tables': [
            {'table': name, 'columns': columns} for name, columns in current['result_tables'].items()],
            **{k: packet[k] for k in ('knowledge',) if k in packet}}
        result['data_sources'] = []
        result['sql_contract'] = {'target_dialect': 'duckdb', 'parameter_style': ':name'}
        for key in ('request', 'confirmed_requirement'):
            if isinstance(result.get(key), dict):
                result[key].pop('logical_plan', None)
        program = result.get('program', {})
        for step in program.get('steps', []):
            if step['name'] != current['name']:
                for key in ('sql', 'parameters', 'data_source_id', 'inputs'):
                    step.pop(key, None)
    elif current.get('engine') == 'source':
        names = {o['object'].get('table') for o in packet.get('objects', [])
                 if o['object'].get('dataSourceId') == current.get('data_source_id')}
        result['materials'] = {**packet,
            'objects': [o for o in packet.get('objects', []) if o['object'].get('dataSourceId') == current['data_source_id']],
            'schemas': [s for s in packet.get('schemas', []) if s.get('object') in names
                        and s.get('dataSourceId') == current['data_source_id']],
            'relations': [r for r in packet.get('relations', []) if r.get('from') in names and r.get('to') in names
                          and r.get('fromDataSourceId') == current['data_source_id']
                          and r.get('toDataSourceId') == current['data_source_id']]}
        result['data_sources'] = [s for s in result.get('data_sources', []) if s['id'] == current['data_source_id']]
    return result


def program_output_contract(payload, original):
    """Constrain engine/source combinations before the model writes SQL.

    Stored SQLProgram stays backwards compatible. The model-facing JSON schema
    has separate branches with concrete execution targets and table scopes.
    """
    schema = deepcopy(original)
    base = schema['$defs']['SQLProgramStep']
    base['properties']['sql']['description'] = '当前执行目标中的完整只读SQL，使用该目标允许的表和列。'
    for key in ('engine', 'data_source_id', 'dialect'):
        base['properties'][key] = {'type': 'string'}
    branches = []
    for index, target in enumerate(execution_scope(payload)['source_targets']):
        name = f'SourceSQLTarget{index}'
        schema['$defs'][name] = {'allOf': [{'$ref': '#/$defs/SQLProgramStep'}, {
            'type': 'object', 'required': ['engine', 'data_source_id'], 'properties': {
                'engine': {'const': 'source'}, 'data_source_id': {'const': target['data_source_id']},
                'dialect': {'const': target['dialect']},
                'sql': {'type': 'string', 'description': '只读execution_scope中本数据源允许的表；前序结果仅通过inputs绑定键值。'}}}]}
        branches.append({'$ref': '#/$defs/' + name})
    schema['$defs']['LocalResultSQL'] = {'allOf': [{'$ref': '#/$defs/SQLProgramStep'}, {
        'type': 'object', 'required': ['engine'], 'properties': {
            'engine': {'const': 'local'}, 'data_source_id': {'const': ''}, 'dialect': {'const': 'duckdb'},
            'inputs': {'type': 'array', 'maxItems': 0},
            'sql': {'type': 'string', 'description': '在DuckDB中计算前序结果；FROM/JOIN只引用前序步骤name，列取该步骤output_columns。'}}}]}
    schema['properties']['steps']['items'] = {'oneOf': [*branches, {'$ref': '#/$defs/LocalResultSQL'}]}
    # A physical-data query first obtains a source result. Pure calculations,
    # with no selected physical catalog, can still be local-only.
    if branches:
        schema['properties']['steps']['prefixItems'] = [{'oneOf': branches}]
    return schema
