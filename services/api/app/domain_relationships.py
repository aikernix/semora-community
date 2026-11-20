"""Business-domain review actions over cross-source records and effective configuration."""
from copy import deepcopy
from uuid import uuid4
from fastapi import HTTPException
import sqlglot
from sqlglot import exp

KNOWN_CARDINALITIES={'1:1','1:N','N:1','N:N'}

def objects_by_name(configuration):
    return {o['name']:o for o in configuration.get('objects',[])}

def cross_relation(relation,objects):
    a,b=objects.get(relation.get('from'),{}),objects.get(relation.get('to'),{})
    return bool(a.get('dataSourceId') and b.get('dataSourceId') and a['dataSourceId']!=b['dataSourceId'])

def records_for_domain(configuration,domain,service):
    ids={r['dataSourceId'] for r in configuration.get('sourcePackageRefs',[])}
    ids.update(o.get('dataSourceId') for o in configuration.get('objects',[]))
    records={}
    for sid in ids-{None,''}:
        for record in service.repository.list_candidates(sid,'cross_source',domain):
            if record['source_datasource_id'] in ids and record['target_datasource_id'] in ids:
                records[record['id']]=record
    return records

def relation_for_record(record,configuration):
    from .datasource_modeling import package_relations
    mapping={}
    for side in ('source','target'):
        obj=next((o for o in configuration.get('objects',[]) if o.get('dataSourceId')==record[side+'_datasource_id']
                  and (o.get('metadata',{}).get('sourceTableId') or o.get('table'))==record[side+'_table_id']),None)
        if not obj:return None
        mapping[side]={'object':obj['name'],**{p.get('column') or p['name']:p['name'] for p in obj.get('properties',[])}}
    manual=next((e.get('relation') for e in reversed(record.get('evidence',[])) if e.get('kind')=='manual_domain_relation'),None)
    if manual:
        if manual.get('from')!=mapping['source']['object'] or manual.get('to')!=mapping['target']['object']:return None
        relation=deepcopy(manual)
        relation['metadata']={**relation.get('metadata',{}),'candidateId':record['id'],'origin':'manual'}
        return relation
    values=package_relations([{**record,'status':'accepted','source_table_id':'source','target_table_id':'target'}],mapping)
    return values[0] if values else None

def review_rows(configuration,domain,service):
    objects=objects_by_name(configuration);rows=[];seen=set()
    for relation in configuration.get('relations',[]):
        if not cross_relation(relation,objects):continue
        identifier=relation.get('metadata',{}).get('candidateId') or relation['name']
        seen.add(identifier)
        rows.append({'id':identifier,'status':'accepted','relation':relation,'decision_reason':''})
    for identifier,record in records_for_domain(configuration,domain,service).items():
        if identifier in seen:continue
        status=record.get('status')
        if status not in {'pending','unresolved','disabled'}:continue
        relation=relation_for_record(record,configuration)
        if relation:
            reason=next((e.get('reason','') for e in reversed(record.get('evidence',[])) if e.get('reason')),'')
            rows.append({'id':identifier,'status':'pending' if status in {'pending','unresolved'} else 'disabled',
                         'relation':relation,'decision_reason':reason})
    return rows

def manual_record(relation,configuration,domain,service,identifier=None):
    from .datasource_modeling import _candidate
    from .query_relationships import validated_guards
    objects=objects_by_name(configuration)
    if not cross_relation(relation,objects):raise HTTPException(422,'本体工作室只维护跨数据库关系')
    if relation.get('cardinality') not in KNOWN_CARDINALITIES:raise HTTPException(422,'请先选择明确的关联基数')
    source,target=objects[relation['from']],objects[relation['to']]
    maps={side:{p['name']:p.get('column') or p['name'] for p in obj.get('properties',[])}
          for side,obj in [('source',source),('target',target)]}
    aliases={relation['from']:'source',relation['to']:'target'}
    pairs=[];guards=[]
    def column(value):
        side=aliases.get(value.table)
        if side is None or value.name not in maps[side]:raise HTTPException(422,'关联条件引用不存在的表或字段')
        return side,maps[side][value.name]
    try:
        tree=sqlglot.parse_one(relation.get('condition',''))
        if tree is None or tree.find(exp.Select):raise HTTPException(422,'请填写有效的关联条件')
        for value in tree.find_all(exp.Column):column(value)
        leaves=list(tree.flatten()) if isinstance(tree,exp.And) else [tree]
        for leaf in leaves:
            while isinstance(leaf,exp.Paren):leaf=leaf.this
            if not isinstance(leaf,exp.EQ):continue
            left,right=leaf.this,leaf.expression
            if isinstance(left,exp.Column) and isinstance(right,exp.Column):
                a,b=column(left),column(right)
                if a[0]==b[0]:raise HTTPException(422,'字段映射必须连接两端表')
                pair={a[0]:a[1],b[0]:b[1]};pairs.append(pair)
            else:
                if not isinstance(left,exp.Column):left,right=right,left
                if not isinstance(left,exp.Column) or not isinstance(right,(exp.Literal,exp.Boolean)):continue
                side,field=column(left)
                value=right.this if isinstance(right,exp.Boolean) or right.is_string else float(right.this) if '.' in right.this else int(right.this)
                guards.append({'side':side,'field':field,'operator':'eq','value':value})
    except (sqlglot.errors.ParseError,ValueError,TypeError):raise HTTPException(422,'关联条件无法解析') from None
    if not pairs or len({p['source'] for p in pairs})!=len(pairs) or len({p['target'] for p in pairs})!=len(pairs):
        raise HTTPException(422,'请提供完整且不重复的两端字段映射')
    table_maps={}
    physical=[]
    for obj in (source,target):
        sid=obj['dataSourceId'];tables=service.datasources.repository.get(sid).scan_result['tables']
        table_maps[sid]={t['id']:t for t in tables}
        tid=obj.get('metadata',{}).get('sourceTableId') or obj['table']
        if tid not in table_maps[sid]:raise HTTPException(422,'数据源表结构已变化，请更新绑定')
        physical.append(table_maps[sid][tid])
    definition=deepcopy(relation)
    definition.setdefault('metadata',{}).pop('relationshipRecord',None)
    record=_candidate(source['dataSourceId'],target['dataSourceId'],physical[0],[p['source'] for p in pairs],
        physical[1],[p['target'] for p in pairs],'cross_source',[{'kind':'manual_domain_relation','reason':'用户编辑并生效','relation':definition}],
        grade='manual',status='accepted',domain=domain)
    from .relationship_inputs import digest
    record_id=identifier if identifier in records_for_domain(configuration,domain,service) else digest([domain,identifier])[:32] if identifier else uuid4().hex
    record.update(id=record_id,key_pairs=pairs,cardinality=relation['cardinality'],reviewed_by='data-steward',research_pending=False)
    checked=validated_guards(record,guards,table_maps)
    if checked is None:raise HTTPException(422,'附加条件不符合实际结构')
    record['condition_template']['guards']=checked
    return record

async def save_domain_relation(domain,identifier,relation,configuration,service,ontology):
    from .datasource_table_storage import tables_digest
    from .relation_output import published_relationship
    record=manual_record(relation,configuration,domain,service,identifier)
    relation=deepcopy(relation)
    relation['metadata']={**relation.get('metadata',{}),'candidateId':record['id'],'origin':'manual',
                          'relationshipRecord':published_relationship(record)}
    relation['metadata'].pop('sourcePackageId',None)
    configuration['relations']=[r for r in configuration.get('relations',[]) if r.get('metadata',{}).get('candidateId')!=identifier
                                and r['name']!=identifier]+[relation]
    saved=await ontology.save_configuration(domain,configuration)
    hashes=[tables_digest(service.datasources.repository.get(record[side+'_datasource_id']).scan_result['tables']) for side in ('source','target')]
    service.repository.replace_candidates([record],*hashes)
    return saved
