<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { BookIcon, JumpIcon } from 'tdesign-icons-vue-next'
import { api, type KnowledgeResearchResult } from '../api'

interface BindingCard { key:string; domain:string; kbId:string; savedDomain:string; savedKbId:string; saving:boolean; error:string }
const status=ref<any>({status:'loading',provider:'weknora'})
const bases=ref<any[]>([]),domains=ref<any[]>([]),cards=ref<BindingCard[]>([])
const loading=ref(false),error=ref(''),message=ref(''),domainErrors=ref<string[]>([])
const anySaving=computed(()=>cards.value.some(card=>card.saving))
const removePrompt=ref<BindingCard|null>(null)
const domainLocks=new Map<string,Promise<unknown>>()
let nextKey=0,loadVersion=0
function domainName(id:string){return domains.value.find(item=>item.domain===id)?.name||'业务域'}
function baseOf(id:string){return bases.value.find(item=>item.id===id)}
function newCard(domain='',kbId=''):BindingCard{return {key:`binding-${++nextKey}`,domain,kbId,savedDomain:kbId?domain:'',savedKbId:kbId,saving:false,error:''}}
function addCard(){cards.value.push(newCard())}
function changed(card:BindingCard){return !card.savedDomain||card.kbId!==card.savedKbId}
function baseOptions(card:BindingCard){
  const options=bases.value.map(base=>({value:base.id,label:base.name,disabled:cards.value.some(other=>other.key!==card.key&&other.savedDomain===card.domain&&other.savedKbId===base.id)}))
  if(card.kbId&&!baseOf(card.kbId))options.push({value:card.kbId,label:'已绑定的知识库（当前不可访问）',disabled:true})
  return options
}
async function serial(domain:string,operation:()=>Promise<void>){
  const work=(domainLocks.get(domain)||Promise.resolve()).catch(()=>{}).then(operation)
  domainLocks.set(domain,work)
  try{await work}finally{if(domainLocks.get(domain)===work)domainLocks.delete(domain)}
}
async function load(){
  if(anySaving.value)return
  const revision=++loadVersion
  loading.value=true;error.value='';status.value={...status.value,status:'loading'}
  try{
    const [s,b,d]=await Promise.all([api.get('/knowledge/status'),api.get('/knowledge-bases'),api.get('/domains')])
    if(revision!==loadVersion)return
    status.value=s.data;bases.value=b.data;domains.value=d.data
    const previous=cards.value
    const results=await Promise.allSettled(domains.value.map(async domain=>({domain:domain.domain,draft:(await api.get(`/ontology/drafts/${encodeURIComponent(domain.domain)}`)).data})))
    if(revision!==loadVersion)return
    const restored:BindingCard[]=[];domainErrors.value=[]
    for(let index=0;index<results.length;index++){
      const result=results[index]!
      if(result.status==='rejected'){
        domainErrors.value.push(domainName(domains.value[index].domain))
        restored.push(...previous.filter(card=>card.savedDomain===domains.value[index].domain))
        continue
      }
      for(const id of new Set<string>(result.value.draft.knowledgeBaseIds||[])){
        restored.push(previous.find(card=>card.savedDomain===result.value.domain&&card.savedKbId===id)||newCard(result.value.domain,id))
      }
    }
    cards.value=[...restored,...previous.filter(card=>!card.savedDomain)]
  }catch(reason:any){if(revision===loadVersion){status.value={...status.value,status:'unavailable'};error.value=reason.response?.data?.detail||'无法读取知识库连接或绑定配置'}}
  finally{if(revision===loadVersion)loading.value=false}
}
async function saveCard(card:BindingCard){
  if(card.saving||!card.domain||!card.kbId||!changed(card))return
  if(!baseOf(card.kbId)){card.error='当前连接无法访问所选知识库，请核对连接配置。';return}
  card.saving=true;card.error='';message.value=''
  try{
    await serial(card.domain,async()=>{
      const current=(await api.get(`/ontology/drafts/${encodeURIComponent(card.domain)}`)).data
      const ids:string[]=current.knowledgeBaseIds||[]
      if(ids.includes(card.kbId)&&card.kbId!==card.savedKbId)throw new Error('该业务域已绑定此知识库，请勿重复添加。')
      const next=[...new Set([...ids.filter(id=>id!==card.savedKbId),card.kbId])]
      await api.patch(`/ontology/drafts/${encodeURIComponent(card.domain)}/knowledge-bases`,{knowledgeBaseIds:next})
      card.savedDomain=card.domain;card.savedKbId=card.kbId
      message.value='知识库绑定已保存，新查询使用更新后的配置。'
    })
  }catch(reason:any){card.error=reason.response?.data?.detail||reason.message||'保存知识库绑定失败'}
  finally{card.saving=false}
}
async function removeBinding(){
  const card=removePrompt.value;if(!card||card.saving)return
  card.saving=true;card.error=''
  try{
    await serial(card.savedDomain,async()=>{
      const current=(await api.get(`/ontology/drafts/${encodeURIComponent(card.savedDomain)}`)).data
      await api.patch(`/ontology/drafts/${encodeURIComponent(card.savedDomain)}/knowledge-bases`,{knowledgeBaseIds:(current.knowledgeBaseIds||[]).filter((id:string)=>id!==card.savedKbId)})
      cards.value=cards.value.filter(item=>item.key!==card.key);removePrompt.value=null
      message.value='已解除此知识库绑定，其他绑定保留。'
    })
  }catch(reason:any){card.error=reason.response?.data?.detail||reason.message||'解除绑定失败'}
  finally{card.saving=false}
}
function removeCard(card:BindingCard){if(card.savedDomain)removePrompt.value=card;else cards.value=cards.value.filter(item=>item.key!==card.key)}

const testOpen=ref(false),testBinding=ref<{domain:string;kbId:string;domainName:string;kbName:string}|null>(null)
const query=ref(''),research=ref<KnowledgeResearchResult|null>(null),testing=ref(false),testError=ref('')
let testVersion=0,requestController:AbortController|undefined
function openTest(card:BindingCard){
  if(!card.savedDomain||changed(card)||!baseOf(card.savedKbId))return
  closeTest();testBinding.value={domain:card.savedDomain,kbId:card.savedKbId,domainName:domainName(card.savedDomain),kbName:baseOf(card.savedKbId).name}
  query.value='';research.value=null;testError.value='';testOpen.value=true
}
function closeTest(){testVersion++;requestController?.abort();testing.value=false;testOpen.value=false}
async function testRetrieval(){
  if(testing.value||!query.value.trim()||!testBinding.value)return
  const revision=++testVersion;const controller=new AbortController();requestController=controller
  testing.value=true;research.value=null;testError.value=''
  try{
    const result=(await api.post<KnowledgeResearchResult>('/knowledge/research',{query:query.value.trim(),knowledge_base_ids:[testBinding.value.kbId]},{timeout:200_000,signal:controller.signal})).data
    if(revision===testVersion)research.value=result
  }catch(reason:any){if(revision===testVersion&&!controller.signal.aborted)testError.value=reason.response?.data?.detail||'知识检索测试失败'}
  finally{if(revision===testVersion)testing.value=false}
}

const configOpen=ref(false),configBusy=ref(false),configMessage=ref('')
const config=ref({base_url:'',api_key:'',api_key_configured:false})
async function openConfig(){
  error.value=''
  try{config.value={...(await api.get('/settings/knowledge')).data,api_key:''};configMessage.value='';configOpen.value=true}
  catch(reason:any){error.value=reason.response?.data?.detail||'无法读取连接配置'}
}
async function configureKnowledge(test=false){
  configBusy.value=true;configMessage.value=''
  try{
    const body={base_url:config.value.base_url,api_key:config.value.api_key}
    if(test){const result=(await api.post('/settings/knowledge/test',body)).data;configMessage.value=`连接成功，可访问 ${result.knowledge_bases} 个知识库。`}
    else{
      const saved=(await api.put('/settings/knowledge',body)).data
      status.value={provider:'weknora',status:'loading',instance_url:saved.base_url.replace(/\/api\/v1$/,''),api_key_configured:saved.api_key_configured}
      config.value.api_key='';bases.value=[];configOpen.value=false;closeTest();message.value='连接配置已保存，请核对业务域的知识库绑定。';await load()
    }
  }catch(reason:any){configMessage.value=reason.response?.data?.detail||'连接配置操作失败'}
  finally{configBusy.value=false}
}
function openWeKnora(){if(status.value.instance_url)window.open(`${status.value.instance_url}/platform/knowledge-bases`,'_blank','noopener')}
onMounted(load)
onUnmounted(()=>{loadVersion++;closeTest()})
</script>

<template>
  <div class="page management-page knowledge-page">
    <div class="page-header">
      <div>
        <div class="knowledge-title">
          <h1>知识库配置</h1>
          <t-tooltip trigger="click" placement="bottom-left" content="知识库是可选项。接入 WeKnora 后，Semora 可检索业务术语、指标口径等资料，辅助理解问题。未配置知识库也可使用查询工作台。">
            <button type="button" class="knowledge-help" aria-label="知识库用途说明">?</button>
          </t-tooltip>
        </div>
      </div>
    </div>
    <t-alert v-if="error" theme="error" :message="error" closable @close="error=''" />
    <t-alert v-if="message" theme="success" :message="message" closable @close="message=''" />
    <section class="provider-section" aria-labelledby="weknora-heading">
      <div class="provider-heading">
        <h2 id="weknora-heading">WeKnora 连接</h2>
        <div class="connection" :class="{ connected: status.status === 'ready' }" aria-label="WeKnora 连接状态">
          <span><i class="status-dot"></i>{{ status.status === 'ready' ? '已连接' : status.status === 'loading' ? '正在连接' : '连接不可用' }}</span>
          <span>{{ bases.length }} 个知识库</span>
          <span>API Key {{ status.api_key_configured ? '已配置' : '未配置' }}</span>
        </div>
        <div class="header-actions">
          <button type="button" class="provider-link" :disabled="anySaving" @click="openConfig">连接配置</button>
          <a class="provider-link" href="https://github.com/Tencent/WeKnora" target="_blank" rel="noopener noreferrer">GitHub 仓库 <JumpIcon aria-hidden="true" /></a>
          <t-button v-if="status.instance_url" theme="primary" @click="openWeKnora">打开 WeKnora <template #suffix><JumpIcon aria-hidden="true" /></template></t-button>
        </div>
      </div>
    </section>
    <section class="bindings-section">
      <div class="binding-heading">
        <h2>业务域与知识库绑定</h2>
        <t-button theme="primary" :disabled="loading || !domains.length || !bases.length" @click="addCard">＋ 添加绑定</t-button>
      </div>
      <t-alert v-if="domainErrors.length" theme="warning" :message="`以下业务域的绑定暂时无法读取：${domainErrors.join('、')}。请刷新重试。`" />
      <div class="binding-grid">
        <div class="binding-columns" aria-hidden="true"><span>业务域</span><span>知识库</span><span>绑定状态</span><span>操作</span></div>
        <article v-for="card in cards" :key="card.key" class="binding-card">
          <label><span class="field-label">业务域</span><strong v-if="card.savedDomain">{{ domains.find(domain => domain.domain === card.domain)?.name || card.domain }}</strong><t-select v-else v-model="card.domain" filterable :disabled="loading||card.saving" :options="domains.map(domain=>({value:domain.domain,label:domain.name}))" @change="card.error=''" /></label>
          <label :class="{ 'saved-knowledge': !changed(card) && baseOf(card.kbId) }"><span class="field-label">知识库</span><t-select v-model="card.kbId" filterable :disabled="loading||card.saving" :options="baseOptions(card)" placeholder="选择知识库" @change="card.error=''" />
          </label>
          <div class="card-heading">
            <span class="binding-status" :class="{ bound: !changed(card) }"><i class="status-dot"></i>{{ !card.savedDomain ? '未保存' : changed(card) ? '待保存' : '已绑定' }}</span>
          </div>
          <div class="card-actions"><t-button v-if="changed(card)" theme="primary" size="small" :loading="card.saving" :disabled="loading||!card.domain||!card.kbId||!changed(card)" @click="saveCard(card)">保存知识库绑定</t-button><t-button v-if="!changed(card)" variant="text" theme="primary" size="small" :disabled="loading||card.saving||!card.savedDomain||changed(card)||!baseOf(card.savedKbId)" aria-label="知识检索测试" @click="openTest(card)">检索测试</t-button><t-button size="small" :theme="card.savedDomain ? 'primary' : 'default'" variant="text" :disabled="loading||card.saving" @click="removeCard(card)">{{card.savedDomain?'解除绑定':'移除'}}</t-button></div>
          <t-alert v-if="card.savedKbId && !baseOf(card.kbId)" theme="warning" message="当前连接无法访问原绑定知识库，请检查连接配置，或重新选择知识库。" />
          <t-alert v-if="card.error" theme="error" :message="card.error" />
        </article>
        <div v-if="!cards.length && loading" class="binding-empty" role="status">正在读取知识库绑定…</div>
        <div v-else-if="!cards.length" class="binding-empty">
          <h3>{{ domainErrors.length ? '部分绑定未能读取' : '尚未建立知识库绑定' }}</h3>
          <p v-if="!domainErrors.length">{{ !domains.length ? '请先在本体工作室创建业务域。' : !bases.length ? '请先连接可访问的知识库。' : '为业务域选择一个知识库。' }}</p>
        </div>
      </div>
    </section>
    <t-drawer :close-btn="true" v-if="testOpen" :visible="testOpen" :close-on-esc-keydown="true" :header="`${testBinding?.domainName||''} · 知识检索测试`" :footer="false" size="min(840px,96vw)" @close="closeTest">
      <div class="retrieval-test management-controls"><div class="test-context"><BookIcon/><span>{{testBinding?.kbName}}</span></div><label>查询需求<t-textarea v-model="query" :disabled="testing" :autosize="{minRows:4,maxRows:10}" placeholder="输入要检索的业务问题或知识需求，可分行补充背景。" /></label><div class="test-actions"><t-button theme="primary" :loading="testing" :disabled="!query.trim()" @click="testRetrieval">开始检索</t-button></div><t-alert v-if="testError" theme="error" :message="testError" />
        <div v-if="research" class="research-result"><section class="answer"><small>检索概况</small><p>{{research.answer}}</p></section><section class="knowledge-points"><h3>检索到的知识点（{{research.evidence.length}}）</h3><article v-for="(item,index) in research.evidence" :key="`${item.knowledge_base_id}-${item.chunk_id}-${index}`"><strong>{{item.title||`知识点 ${index+1}`}}</strong><small>{{[item.locator,item.revision].filter(Boolean).join(' · ')}}</small><p>{{item.summary}}</p></article><p v-if="!research.evidence.length" class="hint">未检索到相关知识点，可补充背景或调整查询描述后重试。</p></section><details v-if="research.steps.length" class="trace"><summary>查看检索过程</summary><article v-for="(step,index) in research.steps" :key="index"><strong>步骤 {{index+1}}</strong><p>{{step.summary}}</p></article></details></div>
        <div v-else-if="!testing" class="test-empty">输入查询需求，检索相关知识点及其来源。</div>
      </div>
    </t-drawer>
    <t-dialog :visible="!!removePrompt" header="解除知识库绑定" :confirm-btn="{content:'解除绑定',theme:'danger',loading:removePrompt?.saving}" :cancel-btn="{disabled:removePrompt?.saving}" :close-btn="!removePrompt?.saving" :close-on-overlay-click="!removePrompt?.saving" @confirm="removeBinding" @close="removePrompt=null" @cancel="removePrompt=null"><p>解除“{{domainName(removePrompt?.savedDomain||'')}}”与“{{baseOf(removePrompt?.savedKbId||'')?.name||'原知识库'}}”的绑定，保留该业务域的其他知识库绑定。</p><t-alert v-if="removePrompt?.error" theme="error" :message="removePrompt.error" /></t-dialog>
    <t-dialog v-model:visible="configOpen" header="WeKnora 连接配置" :footer="false" width="560px"><div class="config-form management-controls"><label>服务地址<t-input v-model="config.base_url" placeholder="例如：http://服务器地址:8088" /></label><label>API Key<t-input v-model="config.api_key" type="password" autocomplete="new-password" :placeholder="config.api_key_configured?'已配置，留空保留原值；更换实例时须重新填写':'填写 WeKnora API Key'" /></label><t-alert v-if="configMessage" :message="configMessage" /><p>更换实例不会自动替换业务域已绑定的知识库，请保存后核对并重新选择。</p><div class="header-actions"><t-button variant="outline" :loading="configBusy" :disabled="!config.base_url" @click="configureKnowledge(true)">测试连接</t-button><t-button theme="primary" :loading="configBusy" :disabled="!config.base_url" @click="configureKnowledge(false)">保存配置</t-button></div></div></t-dialog>
  </div>
</template>

<style scoped>
.knowledge-page { max-width: 1540px; }
.header-actions { display: flex; gap: 8px; flex-wrap: wrap; }
.knowledge-title { display: flex; align-items: center; gap: 10px; margin-bottom: 8px; }
.knowledge-title h1 { margin: 0; }
.knowledge-help { width: 19px; height: 19px; display: grid; place-items: center; padding: 0; border: 1px solid #92a8c2; border-radius: 50%; background: transparent; color: #4f6c8d; font-size: 12px; font-weight: 650; cursor: pointer; }
.knowledge-help:hover, .knowledge-help:focus-visible { color: var(--management-blue); background: var(--management-tint); }
.knowledge-page {
  --management-blue: #0c52a4;
  --td-brand-color-hover: #1c62b3;
  --td-brand-color-active: #0a447f;
  color: #101b2d;
  font-family: Arial, "PingFang SC", "Microsoft YaHei", sans-serif;
  padding: 60px clamp(24px, 2.5vw, 40px);
}
.knowledge-page > .page-header { margin-bottom: 42px; }
.knowledge-title { margin-bottom: 0; }
.knowledge-title h1 { font-size: 32px; font-weight: 700; letter-spacing: 0; }
.provider-section { margin-bottom: 32px; padding-bottom: 36px; border-bottom: 1px solid #cbd3de; }
.provider-heading { display: flex; align-items: center; flex-wrap: wrap; gap: 12px 28px; }
.provider-heading h2, .binding-heading h2 { margin: 0; font-size: 23px; line-height: 1.4; font-weight: 700; }
.provider-heading .header-actions { align-items: center; margin-left: auto; gap: 18px; }
.provider-link {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 0;
  border: 0;
  background: transparent;
  color: var(--management-blue);
  font-family: inherit;
  font-size: 16px;
  font-weight: 400;
  line-height: 22px;
  text-decoration: none;
  cursor: pointer;
}
.provider-link + .provider-link { border-left: 1px solid #cbd5e0; padding-left: 18px; }
.provider-link:hover:not(:disabled), .provider-link:focus-visible { text-decoration: underline; }
.provider-link:disabled { color: var(--semora-muted); cursor: default; }
.provider-heading .t-button, .binding-heading .t-button { font-size: 16px; }
.knowledge-page :deep(.t-button--theme-primary.t-button--variant-base) { border-radius: 4px; min-height: 44px; padding-inline: 17px; }
.knowledge-page > .t-alert, .bindings-section > .t-alert { margin-bottom: 14px; }
.connection { display: flex; flex-wrap: wrap; align-items: center; gap: 6px 0; font-size: 16px; color: #39475a; }
.connection > span { padding-inline: 17px; border-inline-start: 1px solid #cbd5e0; }
.connection > span:first-child { border: 0; padding-inline-start: 0; }
.connection .status-dot, .binding-status .status-dot { box-shadow: none; margin-right: 9px; width: 8px; height: 8px; background: currentColor; }
.connection.connected > span:first-child, .binding-status.bound { color: #12803e; }
.binding-heading { display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 12px; margin-bottom: 20px; }
.binding-grid { --bindings-columns: minmax(0, 24fr) minmax(0, 27fr) minmax(80px, 21fr) minmax(160px, 28fr); border: 1px solid #d4dde7; border-radius: 4px; overflow: hidden; }
.binding-columns, .binding-card { display: grid; grid-template-columns: var(--bindings-columns); align-items: center; gap: 8px 16px; padding: 12px 19px; }
.binding-columns { background: #e6ebf0; color: #253244; font-size: 17px; font-weight: 650; padding-block: 14px; }
.binding-card { border-top: 1px solid #dbe3ec; min-height: 62px; }
.binding-card label, .config-form label, .retrieval-test label { display: flex; flex-direction: column; min-width: 0; gap: 6px; font-size: 14px; }
.binding-card label strong { font-size: 16px; font-weight: 550; overflow-wrap: anywhere; }
.binding-card .field-label { position: absolute; width: 1px; height: 1px; overflow: hidden; clip-path: inset(50%); }
.saved-knowledge :deep(.t-input:not(.t-is-focused)) { border-color: transparent; background: transparent; }
.saved-knowledge :deep(.t-input:not(.t-is-focused):not(:hover) .t-input__suffix) { opacity: 0; }
.saved-knowledge :deep(.t-input) { padding-left: 0; }
.saved-knowledge :deep(.t-input__inner) { font-size: 16px; color: #202d3d; }
.binding-status { font-size: 16px; color: #805516; white-space: nowrap; }
.card-actions { display: flex; flex-wrap: wrap; align-items: center; gap: 4px 10px; }
.card-actions :deep(.t-button--variant-text) { padding-inline: 0; font-size: 16px; }
.card-actions :deep(.t-button--variant-text + .t-button--variant-text)::before { content: ""; display: inline-block; height: 14px; width: 1px; margin-inline-end: 10px; background: #cbd5e0; }
.binding-card > .t-alert { grid-column: 1 / -1; }
.binding-empty { text-align: center; border-top: 1px solid #dbe3ec; padding: 27px 16px; color: var(--semora-muted); font-size: 14px; }
.binding-empty h3 { font-size: 15px; color: var(--semora-ink); margin: 0 0 5px; }
.binding-empty p { margin: 0; font-size: 13px; }
@media (max-width: 850px) {
  .knowledge-page { padding-block: 32px; }
  .knowledge-page > .page-header { margin-bottom: 28px; }
  .provider-section { padding-bottom: 22px; }
  .binding-columns { display: none; }
  .binding-card { grid-template-columns: repeat(2,minmax(0,1fr)); }
  .binding-card .field-label { position: static; width: auto; height: auto; clip-path: none; }
  .provider-heading .header-actions { margin-left: 0; }
}
.retrieval-test{display:flex;flex-direction:column;gap:20px;padding:4px 8px 24px}.test-context{display:flex;align-items:center;gap:10px;padding:14px;background:var(--semora-page);border-radius:8px}.test-actions{display:flex;justify-content:flex-end}.research-result{display:grid;gap:20px;border-top:1px solid var(--semora-line);padding-top:20px}.answer{border-left:3px solid var(--semora-green);padding-left:12px}.answer small,.hint{color:var(--semora-muted);font-size:12px}.answer p,.knowledge-points p{line-height:1.8;white-space:pre-wrap}.knowledge-points h3{font-size:15px}.knowledge-points article{border:1px solid var(--semora-line);border-radius:10px;padding:18px;margin-top:12px}.knowledge-points small{display:block;color:var(--semora-muted);margin-top:7px;font-size:12px}.knowledge-points p{margin-bottom:0;font-size:13px}.trace summary{cursor:pointer;color:var(--semora-muted);font-size:13px}.trace article{padding:12px 0;border-bottom:1px solid var(--semora-line);font-size:12px}.trace p{color:var(--semora-muted);line-height:1.7}.test-empty{padding:44px 20px;text-align:center;color:var(--semora-muted);font-size:13px}.config-form{display:grid;gap:18px}.config-form p{color:var(--semora-muted);font-size:12px;line-height:1.7}
</style>
