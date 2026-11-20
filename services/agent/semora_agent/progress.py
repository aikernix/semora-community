"""Bound consecutive unproductive decisions; retain hashes, never tool values."""
import hashlib
import json
import os

from .model import ModelError


class NoProgressError(ModelError):
    public_message='连续多次分析未取得有效进展，已停止本轮处理；请补充当前缺口或检查模型服务。已有需求与确认口径已保留。'


class ProgressBudget:
    def __init__(self,event,state=None):
        self.event=event
        self.state=state if state is not None else {}
        self.limit=int(os.getenv('AGENT_MAX_NO_PROGRESS','3'))
        if self.limit<1:raise ModelError('AGENT_MAX_NO_PROGRESS 必须大于0')
        self.state.setdefault('streak',0)
        self.state.setdefault('seen',[])
        self.state.setdefault('http_attempts',0)
        self.pending=None

    def check(self):
        if self.state['streak']>=self.limit:
            self.event('agent.progress.exhausted',{'summary':f'连续 {self.limit} 次无进展，停止继续推演；未执行未校验方案','streak':self.state['streak'],'limit':self.limit})
            raise NoProgressError(NoProgressError.public_message)

    def fail(self,reason):
        self.state['streak']+=1
        self.event('agent.progress.updated',{'summary':'本次尝试未取得新证据或有效产物','reason':reason,'streak':self.state['streak'],'limit':self.limit})
        self.check()

    def success(self,reason):
        self.state['streak']=0
        self.event('agent.progress.updated',{'summary':'取得新证据或已校验产物','reason':reason,'streak':0,'limit':self.limit})

    @property
    def must_deliver(self):
        return self.state['streak']>=self.limit-1

    def reject_submission(self):
        self.pending=None
        self.fail('invalid_submission')

    def consume(self,messages):
        pending=self.pending
        self.pending=None
        if pending is None:return
        if not pending:
            self.fail('no_structured_action');return
        call=pending[0]
        if call['name']=='AgentSubmission':
            self.fail('invalid_submission');return
        result=next((m for m in reversed(messages) if getattr(m,'type',None)=='tool' and getattr(m,'tool_call_id',None)==call['id']),None)
        if result is None or getattr(result,'status',None)=='error':
            self.fail('tool_error');return
        try:value=json.loads(result.content)
        except (TypeError,ValueError):
            self.fail('unstructured_observation');return
        if not isinstance(value,dict) or value.get('available') is False or value.get('valid') is False or value.get('unchanged') or value.get('error'):
            self.fail('invalid_or_unchanged_observation');return
        name=call['name']
        keys={'read_object':['object','relations'],'read_schema':['tables'],
              'find_relation_path':['path','relations'],'search_wiki':['pages'],
              'read_wiki':['page'],'read_source':['chunks'],'read_evidence':['evidence'],
              'read_task_artifact':['artifact'],'validate_plan':['plan_summary','structural_checks']}.get(name)
        if name in {'enter_planning','resume_exploration'}:
            self.fail('protocol_transition_without_evidence');return
        material={k:value.get(k) for k in keys} if keys else {k:v for k,v in value.items() if k not in {'message','summary','analysis_goal','candidate_id'}}
        if not any(material.values()):
            self.fail('empty_observation');return
        fingerprint=hashlib.sha256(json.dumps([name,material],sort_keys=True,ensure_ascii=False,default=str).encode()).hexdigest()
        if fingerprint in self.state['seen']:
            self.fail('repeated_observation');return
        self.state['seen']=(self.state['seen']+[fingerprint])[-256:]
        self.success('new_tool_evidence')
