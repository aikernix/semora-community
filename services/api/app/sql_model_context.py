"""Physical-name projection for SQL writers; stored semantic references stay intact."""
from copy import deepcopy
import re
import json

import sqlglot
from sqlglot import exp
from sqlglot.optimizer.scope import traverse_scope

from .query_context_wire import encode_payload


class PhysicalReferences:
    def __init__(self, materials):
        self.objects = {item['object']['name']: item['object']
                        for item in materials.get('objects', [])}
        self.fields = {}
        for name, obj in self.objects.items():
            for prop in obj.get('properties', []):
                self.fields[f"{name}.{prop['name']}"] = (
                    obj.get('table') or name, prop.get('column') or prop['name'])
        self.relations = {rel['name']: rel for rel in materials.get('relations', [])
                          if rel.get('name')}

    def reference(self, value):
        if not isinstance(value, str):
            return value
        if value in self.fields:
            table, column = self.fields[value]
            obj = self.objects[value.rsplit('.', 1)[0]]
            return {'table': table, 'column': column,
                    **({'dataSourceId': obj['dataSourceId']} if obj.get('dataSourceId') else {})}
        if value in self.objects:
            obj = self.objects[value]
            return {'table': obj.get('table') or value,
                    **({'dataSourceId': obj['dataSourceId']} if obj.get('dataSourceId') else {})}
        if value in self.relations:
            return self.expression(self.relations[value].get('condition', ''))
        return value

    def expression(self, value, owner=None):
        if not value:
            return value
        # Exact AST identifiers only: never replace literals or substrings.
        tree = sqlglot.parse_one(value, read='postgres')
        aliases = {}
        for scope in traverse_scope(tree):
            for column in scope.columns:
                current = scope
                source = None
                while current is not None:
                    if column.table:
                        source = current.sources.get(column.table)
                    elif len(current.selected_sources) == 1:
                        source = next(iter(current.selected_sources.values()))[1]
                    if source is not None:
                        break
                    current = current.parent
                if isinstance(source, exp.Table) and source.name in self.objects:
                    aliases[id(column)] = source.name
        for column in list(tree.find_all(exp.Column)):
            qualifier = '.'.join(part.name for part in column.parts[:-1])
            semantic = aliases.get(id(column)) or (qualifier if qualifier in self.objects else None)
            if semantic is None and not qualifier and not tree.find(exp.Select):
                semantic = owner
            if semantic is None:
                continue
            ref = f'{semantic}.{column.name}'
            if ref not in self.fields:
                physical_names = {p.get('column') or p['name'] for p in self.objects[semantic].get('properties', [])}
                if column.name in physical_names:
                    continue
                raise ValueError(f'本体表达式引用不存在的属性：{ref}')
            table, field = self.fields[ref]
            if id(column) in aliases or not qualifier:
                column.set('this', exp.to_identifier(field, quoted=True))
                continue
            physical = sqlglot.parse_one(table, into=exp.Table, read='postgres')
            column.replace(exp.Column(this=exp.to_identifier(field, quoted=True),
                **{k: v.copy() for k, v in physical.args.items()
                   if k in {'db', 'catalog'} and v is not None},
                table=physical.this.copy()))
        for table in list(tree.find_all(exp.Table)):
            if table.name in self.objects:
                physical = sqlglot.parse_one(self.objects[table.name].get('table') or table.name, into=exp.Table, read='postgres')
                if table.args.get('alias'):
                    physical.set('alias', table.args['alias'].copy())
                table.replace(physical)
        return tree.sql(dialect='postgres')

    def structured(self, value):
        """Translate reference slots, preserving user text and encoded values."""
        if isinstance(value, list):
            return [self.structured(v) for v in value]
        if not isinstance(value, dict):
            return value
        result = {}
        list_refs = {'subject_refs', 'grain_refs', 'output_refs', 'dimension_refs',
                     'field_refs', 'relation_refs', 'objects'}
        scalar_refs = {'object', 'target_ref', 'relation_ref', 'ref', 'grain'}
        for key, item in value.items():
            if key in list_refs and isinstance(item, list):
                result[key] = [self.reference(v) for v in item]
            elif key in scalar_refs:
                result[key] = self.reference(item)
            elif key in {'expression', 'condition'} and isinstance(item, str):
                result[key] = self.expression(item)
            elif key == 'message' and isinstance(item, str):
                # Deterministic findings contain explanatory text around known refs.
                refs = {**{k: self.reference(k) for k in self.objects},
                        **{k: self.reference(k) for k in self.fields},
                        **{k: self.reference(k) for k in self.relations}}
                pattern = '|'.join(re.escape(k) for k in sorted(refs, key=len, reverse=True))
                result[key] = re.sub(pattern, lambda m: refs[m.group()] if isinstance(refs[m.group()], str) else json.dumps(refs[m.group()], ensure_ascii=False), item) if pattern else item
            else:
                result[key] = self.structured(item)
        return result

    def object(self, original):
        obj = deepcopy(original)
        name = obj.pop('name')
        obj['table'] = original.get('table') or name
        props = {p['name']: p.get('column') or p['name'] for p in original.get('properties', [])}
        key = obj.get('primaryKey')
        if isinstance(key, list):
            obj['primaryKey'] = [props.get(k, k) for k in key]
        elif isinstance(key, str):
            obj['primaryKey'] = props.get(key, key)
        for prop in obj.get('properties', []):
            prop['column'] = prop.get('column') or prop['name']
            prop.pop('name', None)
            for attr in ('expression', 'defaultFilters', 'filters'):
                if isinstance(prop.get(attr), str):
                    prop[attr] = self.expression(prop[attr], owner=name)
                elif attr in prop:
                    prop[attr] = self.structured(prop[attr])
        for attr in ('projection', 'expression', 'defaultFilters', 'filters'):
            if isinstance(obj.get(attr), str):
                obj[attr] = self.expression(obj[attr], owner=name)
            elif attr in obj:
                obj[attr] = self.structured(obj[attr])
        return obj

    def materials(self, original):
        packet = deepcopy(original)
        packet['objects'] = [{'object': self.object(item['object'])}
                             for item in original.get('objects', [])]
        for schema in packet.get('schemas', []):
            obj = self.objects.get(schema.get('object'), {})
            schema['object'] = obj.get('table') or schema.get('object')
            if obj.get('dataSourceId'):
                schema['dataSourceId'] = obj['dataSourceId']
        for relation in packet.get('relations', []):
            relation.pop('name', None)
            for end in ('from', 'to'):
                obj = self.objects.get(relation.get(end), {})
                if obj.get('dataSourceId'):
                    relation[end + 'DataSourceId'] = obj['dataSourceId']
                if end in relation:
                    relation[end] = obj.get('table') or relation[end]
            if relation.get('condition'):
                relation['condition'] = self.expression(relation['condition'])
        packet['metrics'] = self.structured(packet.get('metrics', []))
        # The broader identity directory is for object selection, already done.
        # Selected objects retain their own complete keys and semantic definitions.
        packet.pop('identity_catalog', None)
        packet.pop('selected_objects', None)
        return packet


def sql_model_payload(payload, *, reference_materials=None):
    """Only SQL generation/review/repair use this projection, never analysis."""
    refs = PhysicalReferences(reference_materials or payload.get('materials', {}))
    result = deepcopy(payload)
    if isinstance(result.get('materials'), dict):
        result['materials'] = refs.materials(result['materials'])
    for key in ('request', 'confirmed_requirement', 'logical_plan', 'deterministic_alignment_findings'):
        if isinstance(result.get(key), (dict, list)):
            result[key] = refs.structured(result[key])
    from .sql_execution_scope import scoped_payload
    from .sql_input_compaction import compact_sql_input
    return encode_payload(compact_sql_input(scoped_payload(result)))
