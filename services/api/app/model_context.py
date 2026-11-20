"""Project knowledge references into model inputs; keep provenance in query state."""
from copy import deepcopy
import hashlib
import json

def project_knowledge(value, run):
    references=run.agent_memory.setdefault('model_evidence_refs',[{}])[-1]

    def register(source,content):
        source={**source,'content_hash':hashlib.sha256(str(content).encode()).hexdigest()}
        encoded=json.dumps(source,sort_keys=True,ensure_ascii=False,default=str)
        ref='K'+hashlib.sha256(encoded.encode()).hexdigest()[:12]
        references.setdefault(ref,deepcopy(source))
        return ref

    def evidence(item):
        if not isinstance(item,dict):return item
        if item.get('ref') and 'content' in item:return deepcopy(item)
        if 'reference' in item:
            result=deepcopy(item);result['reference']=evidence(item['reference']);return result
        if not any(k in item for k in ('document_id','knowledge_base_id','locator')):
            return visit(item)
        source={k:deepcopy(item[k]) for k in ('knowledge_base_id','document_id','chunk_id','locator','revision','title') if k in item}
        content=item.get('summary',item.get('content',''))
        result={'ref':register(source,content),'content':content,'status':item.get('definition_status',item.get('status','reference'))}
        for k in ('title','scope','conflicts','pending_confirmation','excerpted','original_chars','read_more'):
            if k in item:result[k]=deepcopy(item[k])
        return result

    def knowledge(item):
        if not isinstance(item,dict):return [item]
        if item.get('ref') and 'content' in item:return [deepcopy(item)]
        source={k:deepcopy(item[k]) for k in ('knowledge_base_id','slug','title','revision','source_refs','links') if k in item}
        content=item.get('content','')
        result={'ref':register(source,content),'content':content,'status':item.get('definition_status',item.get('status','reference'))}
        for k in ('title','scope','conflicts','pending_confirmation','excerpted','original_chars','read_more'):
            if k in item:result[k]=deepcopy(item[k])
        rows=[result]
        for original in item.get('sources') or []:
            for chunk in original.get('chunks') or []:
                rows.append({'ref':register({'knowledge_base_id':item.get('knowledge_base_id'),
                    'document_id':original.get('document_id'),'chunk_id':chunk.get('id')},chunk.get('content','')),
                    'content':chunk.get('content',''),'status':'reference'})
        return rows

    def visit(node):
        if isinstance(node,list):return [visit(v) for v in node]
        if not isinstance(node,dict):return node
        result={}
        for key,child in node.items():
            if key=='evidence' and isinstance(child,list):result[key]=[evidence(v) for v in child]
            elif key=='knowledge' and isinstance(child,list):result[key]=[r for v in child for r in knowledge(v)]
            else:result[key]=visit(child)
        return result
    return visit(value)
