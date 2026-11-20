<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { api } from '../api'
import { hasRole } from '../auth'
import DeleteResourceDialog from '../components/DeleteResourceDialog.vue'
import { ontologyNames } from '../ontologyNames'
import { fieldPairs, mappingCondition, type FieldPair } from '../relationshipCondition'
import DatabaseIcon from '../components/DatabaseIcon.vue'
import RelationshipCard from '../components/RelationshipCard.vue'

const domains = ref<any[]>([])
const selectedDomain = ref('')
const ontology = ref<any>(null)
const dataSources = ref<any[]>([])
const names = computed(()=>ontologyNames(ontology.value,dataSources.value))
const sourcePackages = ref<Record<string, any>>({})
const sourcePickerVisible = ref(false)
const sourceToAdd = ref('')
const unbindingSource = ref<any>(null)
const domainRelationRows = ref<any[]>([])
const relationFilter = ref('all')
const editingDomainRelationId = ref('')
const reviewCounts = computed(() => Object.fromEntries(['accepted','pending','disabled'].map(status=>[status,domainRelationRows.value.filter(row=>row.status===status).length])))
const pairKey=(a:string,b:string)=>JSON.stringify([a,b].sort())
const selectedPairKey=ref('')
const pairDrawerVisible=ref(false)
const pairPickerVisible=ref(false)
const crossJobs=ref<Record<string,any>>({})
const startingPair=ref('')
const analysisConfirmation=ref<any>(null)
const cancellingPair=ref('')
const terminalJob=(job:any)=>!job||['completed','completed_with_pending','failed','cancelled'].includes(job.status)
const sourcePairs=computed(()=>boundSources.value.flatMap((left,index)=>boundSources.value.slice(index+1).map(right=>{
  const key=pairKey(left.id,right.id)
  const rows=domainRelationRows.value.filter(row=>pairKey(sourceOf(row.relation.from),sourceOf(row.relation.to))===key)
  return {key,left,right,rows,job:crossJobs.value[key],counts:Object.fromEntries(['accepted','pending','disabled'].map(status=>[status,rows.filter(row=>row.status===status).length]))}
})).filter(pair=>pair.job || pair.rows.length))
const registeredPairs = computed(() => new Set(sourcePairs.value.map(pair => pair.key)))
const availablePairPartners = (sourceId: string) => crossSources.value.filter(
  source => source.id !== sourceId && !registeredPairs.value.has(pairKey(sourceId, source.id)),
)
const pairLeftOptions = computed(() => crossSources.value.filter(source => availablePairPartners(source.id).length > 0))
const pairRightOptions = computed(() => availablePairPartners(crossLeft.value))
const canAddPair = computed(() => !!crossLeft.value && pairRightOptions.value.some(source => source.id === crossRight.value))
const selectedPair=computed(()=>sourcePairs.value.find(pair=>pair.key===selectedPairKey.value))
const pairRows=computed(()=>selectedPair.value?.rows||[])
const pairCounts=computed(()=>selectedPair.value?.counts||{})
const visibleRelations = computed(() => pairRows.value.filter(row=>relationFilter.value==='all'||row.status===relationFilter.value))
const relationPage=ref(1)
const pagedRelations=computed(()=>visibleRelations.value.slice((relationPage.value-1)*20,relationPage.value*20))
const pairObjects=computed(()=>(ontology.value?.objects||[]).filter((obj:any)=>selectedPair.value&&[selectedPair.value.left.id,selectedPair.value.right.id].includes(obj.dataSourceId)))
const knownCardinalities=['1:1','1:N','N:1','N:N']
const crossLeft = ref('')
const crossRight = ref('')
let crossTimer: ReturnType<typeof setTimeout> | undefined
const saving = ref(false)
const error = ref('')
const message = ref('')
const relationVisible = ref(false)
const relationIndex = ref(-1)
const relationDraft = ref<any>({})
const relationError = ref('')
const relationPairs=ref<FieldPair[]>([])
const pairMappingMode=ref(true)
const extraCondition=ref('')
const advancedConditions=ref(false)
const conditionPreview=computed(()=>names.value.display(mappingCondition(relationDraft.value.from,relationDraft.value.to,relationPairs.value)))
function setRelationFields(relation:any){
  const pairs=fieldPairs(relation.condition,relation.from,relation.to)
  pairMappingMode.value=!relation.condition||!!pairs
  relationPairs.value=pairs||[{source:'',target:''}]
  extraCondition.value='';advancedConditions.value=!pairMappingMode.value
}
function resetRelationFields(){setRelationFields({...relationDraft.value,condition:''})}
const deletingRelation = ref<any>(null)
const createVisible = ref(false)
const creating = ref(false)
const createError = ref('')
const newDomain = ref({ name: '', domain: '' })
const deletingDomain = ref<{ domain: string; name: string } | null>(null)
const deletionBusy = ref(false)
const deletionError = ref('')
function openDeleteDomain() {
  if (!ontology.value) return
  deletionError.value = ''
  deletingDomain.value = { domain: ontology.value.domain, name: ontology.value.name }
}
async function deleteDomain() {
  const item = deletingDomain.value
  if (!item || deletionBusy.value || !hasRole('admin')) return
  deletionBusy.value = true
  deletionError.value = ''
  try {
    await api.delete(`/ontology/domains/${encodeURIComponent(item.domain)}`)
  } catch (cause: any) {
    deletionError.value = cause.response?.data?.detail || '删除业务域失败'
    return
  } finally { deletionBusy.value = false }
  deletingDomain.value = null
  if (selectedDomain.value === item.domain) {
    selectedDomain.value = ''
    ontology.value = null
  }
  await load()
  message.value = `已删除业务域「${item.name}」。`
}
let loadVersion = 0

function mapped(value: any) {
  return { ...value, relations: value.relations || [], objects: (value.objects || []).map((object: any) => ({
    ...object, dataSourceId: object.dataSourceId || value.dataSourceId || '',
  })) }
}
function editable(value: any) {
  const { knowledgeBaseIds, version, dataSourceId, ...configuration } = value
  return configuration
}
async function loadDomains() {
  const [items, sources] = await Promise.all([api.get('/ontology/configurations'), api.get('/datasources')])
  domains.value = items.data
  dataSources.value = sources.data
  const packages: Record<string, any> = {}
  await Promise.all(dataSources.value.map(async source => {
    try { packages[source.id] = (await api.get(`/datasources/${source.id}/source-ontology`)).data }
    catch (reason: any) { if (reason.response?.status !== 404) throw reason }
  }))
  sourcePackages.value = packages
  if (!domains.value.some(item => item.domain === selectedDomain.value)) selectedDomain.value = domains.value[0]?.domain || ''
}
const boundSourceIds = computed(() => new Set([
  ...(ontology.value?.sourcePackageRefs || []).map((item: any) => item.dataSourceId),
  ...(ontology.value?.objects || []).map((item: any) => item.dataSourceId || ontology.value?.dataSourceId),
].filter(Boolean)))
function isModeledSource(sourceId: string) {
  return ['ready', 'ready_with_pending'].includes(sourcePackages.value[sourceId]?.status)
}
const crossSources = computed(() => dataSources.value.filter(source => boundSourceIds.value.has(source.id) && isModeledSource(source.id)))
const boundSources = computed(() => dataSources.value.filter(source => boundSourceIds.value.has(source.id)))
const availableSources = computed(() => dataSources.value.filter(source => !boundSourceIds.value.has(source.id) && isModeledSource(source.id)))
const canBindSource = computed(() => availableSources.value.some(source => source.id === sourceToAdd.value))
watch([relationFilter,selectedPairKey],()=>relationPage.value=1)
watch(()=>visibleRelations.value.length,total=>relationPage.value=Math.min(relationPage.value,Math.max(1,Math.ceil(total/20))))
function openSourcePicker() { sourceToAdd.value = ''; sourcePickerVisible.value = true }
async function bindSource(sourceId: string) {
  if (saving.value || !availableSources.value.some(source => source.id === sourceId)) return
  saving.value = true; error.value = ''
  try { ontology.value = mapped((await api.post(`/ontology/configurations/${selectedDomain.value}/sources/${sourceId}`)).data); await loadDomainRelations(); sourcePickerVisible.value = false; sourceToAdd.value = ''; message.value = '数据源已添加到当前业务域。' }
  catch (reason: any) { error.value = reason.response?.data?.detail || '绑定数据源失败' }
  finally { saving.value = false }
}
async function loadDomainRelations() {
  const domain=selectedDomain.value
  const result=(await api.get(`/ontology/configurations/${domain}/relationships`)).data
  if(domain===selectedDomain.value)domainRelationRows.value=result.items||[]
}
async function unbindSource() {
  const source=unbindingSource.value
  if(!source)return
  saving.value=true;error.value=''
  try {
    ontology.value=mapped((await api.delete(`/ontology/configurations/${selectedDomain.value}/sources/${source.id}`)).data)
    if(crossLeft.value===source.id)crossLeft.value=''
    if(crossRight.value===source.id)crossRight.value=''
    unbindingSource.value=null
    await loadDomainRelations()
    crossJobs.value = Object.fromEntries(Object.entries(crossJobs.value).filter(([, job]) =>
      job.datasource_id !== source.id && job.peer_datasource_id !== source.id))
    if(!selectedPair.value)pairDrawerVisible.value=false
    message.value='已解除当前业务域的绑定，相关跨库关系已移除。数据源本身保留。'
  } catch(reason:any){error.value=reason.response?.data?.detail||'解除绑定失败'}
  finally{saving.value=false}
}
async function reviewRelation(row:any,action:string) {
  saving.value=true;error.value=''
  try{
    ontology.value=mapped((await api.post(`/ontology/configurations/${selectedDomain.value}/relationships/${row.id}/decision`,{action})).data)
    await loadDomainRelations();message.value='关联关系已更新。'
  }catch(reason:any){error.value=reason.response?.data?.detail||'关系更新失败'}
  finally{saving.value=false}
}
function editReviewRelation(row:any) {
  openRelation(ontology.value.relations.findIndex((r:any)=>r.name===row.relation.name))
  relationDraft.value=JSON.parse(JSON.stringify(row.relation))
  relationDraft.value.condition=names.value.display(row.relation.condition)
  setRelationFields(row.relation)
  editingDomainRelationId.value=row.id
}
function openPair(pair:any){selectedPairKey.value=pair.key;relationFilter.value='all';pairDrawerVisible.value=true}
function openPairPicker(){error.value='';crossLeft.value='';crossRight.value='';pairPickerVisible.value=true}
function canAnalyze(pair:any){return !saving.value&&!startingPair.value&&terminalJob(pair.job)&&[pair.left.id,pair.right.id].every(id=>crossSources.value.some(s=>s.id===id))}
function requestAnalysis(pair:any){if(canAnalyze(pair)){error.value='';analysisConfirmation.value=pair}}
async function resumeCrossAnalysis(pair:any){
  if(!pair.job||!canAnalyze(pair))return
  const domain=selectedDomain.value,version=loadVersion,id=pair.job.id
  startingPair.value=pair.key;error.value=''
  try{
    const job=(await api.post(`/datasources/${pair.job.datasource_id}/onboarding/${id}/resume`)).data
    if(domain!==selectedDomain.value||version!==loadVersion)return
    crossJobs.value[pair.key]=job;message.value='已继续上次跨源分析。';void pollCross()
  }catch(reason:any){if(domain===selectedDomain.value)error.value=reason.response?.data?.detail||'继续分析失败'}
  finally{if(domain===selectedDomain.value&&version===loadVersion)startingPair.value=''}
}
function pairState(pair:any){return pair.job?.status==='cancelling'?'正在取消':!terminalJob(pair.job)?'正在分析':pair.job?.status==='cancelled'?'已取消':pair.job?.status==='failed'?'分析失败':pair.job?.details?.technical_incomplete?'部分分析未完成':pair.counts.pending?'有待审核':pair.rows.length?'已有关联':pair.job?'已分析':'未分析'}
function canCancel(pair:any){return !terminalJob(pair.job)&&pair.job.phase!=='publishing'&&pair.job.status!=='cancelling'&&!cancellingPair.value}
async function cancelCrossAnalysis(pair:any){
  if(!canCancel(pair))return
  const domain=selectedDomain.value,version=loadVersion,id=pair.job.id
  cancellingPair.value=pair.key;error.value=''
  try{
    const job=(await api.post(`/ontology/configurations/${domain}/cross-source-analysis/${id}/cancel`)).data
    if(domain!==selectedDomain.value||version!==loadVersion)return
    if(crossJobs.value[pair.key]?.id===id)crossJobs.value[pair.key]=job
    message.value=job.status==='cancelled'?'分析已取消，原有已生效关系保留。':terminalJob(job)?'分析任务已结束。':'正在取消分析，请稍候。'
    void pollCross()
  }catch(reason:any){if(domain===selectedDomain.value&&version===loadVersion)error.value=reason.response?.data?.detail||'取消分析失败'}
  finally{if(domain===selectedDomain.value&&version===loadVersion)cancellingPair.value=''}
}
async function pollCross() {
  if(crossTimer)clearTimeout(crossTimer)
  const domain=selectedDomain.value,version=loadVersion
  const jobs=Object.entries(crossJobs.value).filter(([,job])=>!terminalJob(job))
  let finished=false
  await Promise.all(jobs.map(async([key,job])=>{
    try{
      const result=(await api.get(`/ontology/configurations/${domain}/cross-source-analysis/${job.id}`)).data
      if(domain!==selectedDomain.value||version!==loadVersion)return
      if(crossJobs.value[key]?.id!==job.id || terminalJob(crossJobs.value[key])&&!terminalJob(result))return
      crossJobs.value[key]=result
      if(terminalJob(result)){finished=true;if(result.error)error.value=result.error}
    }catch(reason:any){if(domain===selectedDomain.value&&version===loadVersion)error.value=reason.response?.data?.detail||'无法读取跨源分析状态'}
  }))
  if(domain!==selectedDomain.value||version!==loadVersion)return
  if(finished){
    try{const result=await api.get(`/ontology/configurations/${domain}`);if(domain!==selectedDomain.value||version!==loadVersion)return;ontology.value=mapped(result.data);await loadDomainRelations()}
    catch(reason:any){error.value=reason.response?.data?.detail||'无法更新关联结果'}
  }
  if(Object.values(crossJobs.value).some(job=>!terminalJob(job)))crossTimer=setTimeout(()=>void pollCross(),1500)
}
async function analyzeCrossSource(left=crossLeft.value,right=crossRight.value) {
  if(!left||!right||left===right||startingPair.value)return
  const reanalyze = !!analysisConfirmation.value
  if(!reanalyze && !canAddPair.value)return
  const domain=selectedDomain.value,version=loadVersion,key=pairKey(left,right)
  startingPair.value=key;error.value=''
  try{
    const job=(await api.post(`/ontology/configurations/${domain}/cross-source-analysis`,{left_source_id:left,right_source_id:right,...(reanalyze ? { mode: 'reanalyze' } : {})})).data
    if(domain!==selectedDomain.value||version!==loadVersion)return
    crossJobs.value[key]=job;pairPickerVisible.value=false;analysisConfirmation.value=null;message.value='已开始分析所选两个数据源的关联。';void pollCross()
  }catch(reason:any){if(domain===selectedDomain.value)error.value=reason.response?.data?.detail||'无法启动跨源分析'}
  finally{if(domain===selectedDomain.value&&version===loadVersion)startingPair.value=''}
}
function crossMessage(job: any) {
  const details = job.details || {}
  const parts = [`跨源分析：${job.phase} · ${job.progress}%`]
  if (details.shared_keyword_count !== undefined) parts.push(`双边关键词 ${details.shared_keyword_count} 个`)
  if (details.cross_candidate_pairs !== undefined) parts.push(`候选表对 ${details.cross_candidate_pairs} 组`)
  if (details.accepted !== undefined) parts.push(`已生效 ${details.accepted} 条`)
  if (details.research_pending) parts.push(`待复核 ${details.research_pending} 项`)
  return parts.join(' · ')
}
function selectDomain(domain: string) {
  if (selectedDomain.value === domain) return
  selectedDomain.value = domain
  void loadDomain()
}

async function loadDomain() {
  const domain = selectedDomain.value
  const version = ++loadVersion
  if(crossTimer)clearTimeout(crossTimer)
  crossJobs.value={};selectedPairKey.value='';pairDrawerVisible.value=false;pairPickerVisible.value=false;startingPair.value='';analysisConfirmation.value=null;cancellingPair.value=''
  ontology.value = null
  domainRelationRows.value=[]
  relationFilter.value='all'
  unbindingSource.value=null
  sourcePickerVisible.value = false
  relationVisible.value = false
  deletingRelation.value = null
  message.value = ''
  error.value = ''
  if (!domain) return
  try {
    const result = (await api.get(`/ontology/configurations/${domain}`)).data
    if (version === loadVersion && domain === selectedDomain.value) {
      ontology.value = mapped(result);await loadDomainRelations()
      const jobs=(await api.get(`/ontology/configurations/${domain}/cross-source-analysis`)).data.items||[]
      if(version!==loadVersion||domain!==selectedDomain.value)return
      crossJobs.value=Object.fromEntries(jobs.map((job:any)=>[pairKey(job.datasource_id,job.peer_datasource_id),job]));void pollCross()
    }
  } catch (reason: any) { error.value = reason.response?.data?.detail || '无法读取本体配置' }
}
async function load() {
  try { await loadDomains(); await loadDomain() }
  catch (reason: any) { error.value = reason.response?.data?.detail || '无法读取业务域' }
}
function openCreate() {
  newDomain.value = { name: '', domain: '' }
  createError.value = ''
  createVisible.value = true
}
async function createDomain() {
  const domain = newDomain.value.domain.trim()
  const name = newDomain.value.name.trim()
  if (!name || !/^[a-z][a-z0-9_]{1,99}$/.test(domain)) {
    createError.value = '请填写业务域名称；标识为2～100位小写字母、数字或下划线，并以字母开头。'
    return
  }
  creating.value = true
  createError.value = ''
  try {
    await api.post('/ontology/domains', { name, domain })
    selectedDomain.value = domain
    createVisible.value = false
    await load()
    message.value = '业务域已创建，请配置对象与关联关系。'
  } catch (reason: any) { createError.value = reason.response?.data?.detail || '创建业务域失败' }
  finally { creating.value = false }
}
function objectOf(name: string) { return ontology.value?.objects.find((object: any) => object.name === name) }
function tableLabel(name: string) {
  const object = objectOf(name)
  return object ? `${object.label || object.table} · ${object.table}` : name
}
function sourceOf(name: string) { return objectOf(name)?.dataSourceId || '' }
function relationColumns(relation:any,side:'source'|'target'){
  const pairs=fieldPairs(relation.condition,relation.from,relation.to)
  const object=objectOf(side==='source'?relation.from:relation.to)
  if(pairs)return pairs.map(pair=>object?.properties.find((p:any)=>p.name===pair[side])?.column||pair[side])
  return relation.metadata?.relationshipRecord?.[side+'_columns']||[]
}
function sourceLabel(name: string) {
  return dataSources.value.find(source => source.id === sourceOf(name))?.name || sourceOf(name) || '未配置数据源'
}
function openRelation(index = -1) {
  editingDomainRelationId.value=''
  relationIndex.value = index
  relationDraft.value = index < 0
    ? { name: `r_${Array.from(crypto.getRandomValues(new Uint8Array(16)),value=>value.toString(16).padStart(2,'0')).join('')}`, label: '手动添加的关联', from: pairObjects.value.find((o:any)=>o.dataSourceId===selectedPair.value?.left.id)?.name||'', to:pairObjects.value.find((o:any)=>o.dataSourceId===selectedPair.value?.right.id)?.name||'', cardinality: 'N:1', joinType: 'many_to_one', condition: '', optional: false }
    : JSON.parse(JSON.stringify(ontology.value.relations[index]))
  relationError.value = ''
  setRelationFields(relationDraft.value)
  relationDraft.value.condition=names.value.display(relationDraft.value.condition)
  relationVisible.value = true
}
async function saveConfiguration(value: any) {
  const domain = selectedDomain.value
  if (!value || value.domain !== domain) throw new Error('业务域标识不能在编辑过程中更改')
  const result = (await api.put(`/ontology/configurations/${domain}`, value)).data
  if (domain === selectedDomain.value) {
    ontology.value = mapped(result)
  }
  await loadDomains()
  await loadDomainRelations()
  message.value = '本体配置已保存，新查询立即使用；已开始的查询保留原配置。'
}
async function saveRelation() {
  if(pairMappingMode.value&&relationPairs.value.some(pair=>!pair.source||!pair.target)){
    relationError.value='请为每一行选择完整的来源字段和目标字段。';return
  }
  if(pairMappingMode.value&&(new Set(relationPairs.value.map(pair=>pair.source)).size!==relationPairs.value.length||new Set(relationPairs.value.map(pair=>pair.target)).size!==relationPairs.value.length)){
    relationError.value='字段映射不能重复，请检查每一行。';return
  }
  const condition=pairMappingMode.value
    ? [mappingCondition(relationDraft.value.from,relationDraft.value.to,relationPairs.value),extraCondition.value.trim()?`(${names.value.internal(extraCondition.value.trim())})`:''].filter(Boolean).join(' AND ')
    : names.value.internal(relationDraft.value.condition.trim())
  const relation = { ...relationDraft.value, name: relationDraft.value.name.trim(), label:relationDraft.value.label?.trim()||'手动添加的关联', condition }
  if (!/^[a-zA-Z_][a-zA-Z0-9_]*$/.test(relation.name) || !relation.from || !relation.to || !relation.condition) {
    relationError.value = '请选择两端表并补齐字段映射。'
    return
  }
  if(!sourceOf(relation.from)||!sourceOf(relation.to)||sourceOf(relation.from)===sourceOf(relation.to)){
    relationError.value='请选择两个不同数据源的表；同源关系请在数据源页面维护。';return
  }
  if(!knownCardinalities.includes(relation.cardinality)){relationError.value='请先选择明确的关联基数。';return}
  if (ontology.value.relations.some((item: any, index: number) => index !== relationIndex.value && item.name === relation.name)) {
    relationError.value = '保存失败，请重新打开弹窗后重试。'
    return
  }
  const cardinalities: Record<string, string> = { '1:1': 'one_to_one', '1:N': 'one_to_many', 'N:1': 'many_to_one', 'N:N': 'many_to_many' }
  if (relationIndex.value < 0 || relation.cardinality !== ontology.value.relations[relationIndex.value].cardinality) {
    relation.joinType = cardinalities[relation.cardinality]
  }
  const relations = [...ontology.value.relations]
  if (relationIndex.value < 0) relations.push(relation)
  else relations[relationIndex.value] = relation
  saving.value = true
  relationError.value = ''
  try {
    if(editingDomainRelationId.value){
      ontology.value=mapped((await api.put(`/ontology/configurations/${selectedDomain.value}/relationships/${editingDomainRelationId.value}`,relation)).data)
      await loadDomainRelations();message.value='跨库关系已保存并生效。'
    }else await saveConfiguration({ ...editable(ontology.value), relations })
    relationVisible.value = false
  } catch (reason: any) { relationError.value = reason.response?.data?.detail || reason.message || '关系保存失败' }
  finally { saving.value = false }
}
async function deleteRelation() {
  if (!deletingRelation.value) return
  const row=domainRelationRows.value.find(row=>row.relation.name===deletingRelation.value.name)
  if(row)await reviewRelation(row,'disable')
  if(!error.value)deletingRelation.value=null
}
onMounted(load)
onUnmounted(() => { if (crossTimer) clearTimeout(crossTimer) })
</script>

<template>
  <div class="page management-page ontology-page">
    <div class="page-header">
      <h1>本体工作室</h1>
      <div class="header-actions">
        <t-button theme="primary" :disabled="saving" @click="openCreate">新建业务域</t-button>
      </div>
    </div>
    <t-alert v-if="error" theme="error" :message="error" style="margin-bottom:16px" />
    <t-alert v-if="message" theme="success" :message="message" closable @close="message = ''" style="margin-bottom:16px" />
    <div v-if="domains.length" class="domain-bar">
      <nav aria-label="业务域" class="domain-switcher">
        <button v-for="item in domains" :key="item.domain" type="button"
                :aria-current="item.domain === selectedDomain ? 'true' : undefined" :disabled="saving"
                @click="selectDomain(item.domain)">{{ item.name }}</button>
      </nav>
      <t-button v-if="hasRole('admin') && ontology" theme="danger" variant="text" :disabled="saving" @click="openDeleteDomain">删除业务域</t-button>
    </div>
    <template v-if="ontology">
      <section class="domain-summary">
        <div class="domain-heading"><span>当前业务域 <strong>{{ ontology.name }}</strong></span><t-tag variant="light" :theme="ontology.objects.length ? 'success' : 'default'">{{ ontology.objects.length ? '已配置' : '待配置' }}</t-tag></div>
        <div class="ontology-stats"><span>表映射 <b>{{ ontology.objects.length }}</b></span><span>已生效跨库关系 <b>{{ reviewCounts.accepted || 0 }}</b></span></div>
      </section>
      <section class="source-package-panel">
        <div class="section-heading"><h2 class="panel-title">已绑定数据源</h2><t-button variant="outline" :disabled="saving" @click="openSourcePicker">＋ 添加数据源</t-button></div>
        <div v-if="boundSources.length" class="ontology-table-wrap">
          <table class="ontology-table source-table" aria-label="已绑定数据源">
            <thead><tr><th scope="col">数据源</th><th scope="col">对象数</th><th scope="col">关系数</th><th scope="col">状态</th><th scope="col">操作</th></tr></thead>
            <tbody>
              <tr v-for="source in boundSources" :key="source.id" class="package-row">
                <th scope="row"><div class="package-source"><DatabaseIcon :type="source.type" /><strong>{{ source.name }}</strong></div></th>
                <td>{{ sourcePackages[source.id]?.object_count || 0 }}</td>
                <td>{{ sourcePackages[source.id]?.relation_count || 0 }}</td>
                <td><span class="table-status" :class="{ ready: ['ready', 'ready_with_pending'].includes(sourcePackages[source.id]?.status) }">{{ ['ready', 'ready_with_pending'].includes(sourcePackages[source.id]?.status) ? '已完成' : sourcePackages[source.id]?.status === 'stale' ? '需更新' : '尚未完成建模' }}</span></td>
                <td><t-button size="small" variant="text" theme="danger" :disabled="saving" @click="unbindingSource=source">解除绑定</t-button></td>
              </tr>
            </tbody>
          </table>
        </div>
        <div v-else class="empty-hint">尚未添加数据源，点击“＋ 添加数据源”选择。</div>
      </section>
      <section class="model-panel">
        <div class="section-heading">
          <div>
            <h2 class="panel-title">数据源之间的关联</h2>
            <p class="association-hint">数据源关联用于跨数据源联合查询。未配置关联时，单数据源查询仍可正常使用。</p>
          </div>
          <t-button theme="primary" :disabled="!pairLeftOptions.length || saving || !!startingPair" @click="openPairPicker">新增关联</t-button>
        </div>
        <div v-if="sourcePairs.length" class="ontology-table-wrap">
          <table class="ontology-table association-table" aria-label="数据源之间的关联">
            <thead><tr><th scope="col">关联数据源</th><th scope="col">已生效</th><th scope="col">待审核</th><th scope="col">已停用</th><th scope="col">状态</th><th scope="col">操作</th></tr></thead>
            <tbody v-for="pair in sourcePairs" :key="pair.key" class="source-pair-card">
              <tr>
                <th scope="row">
                  <span class="pair-endpoints"><strong>{{ pair.left.name }}</strong><span aria-hidden="true">↔</span><strong>{{ pair.right.name }}</strong></span>
                </th>
                <td>{{ pair.counts.accepted }}</td>
                <td>{{ pair.counts.pending }}</td>
                <td>{{ pair.counts.disabled }}</td>
                <td><span class="table-status" :class="{ running: !terminalJob(pair.job), failed: pair.job?.status === 'failed', pending: terminalJob(pair.job) && pair.counts.pending && pair.job?.status !== 'failed', ready: terminalJob(pair.job) && !pair.counts.pending && pair.rows.length && pair.job?.status !== 'failed' }">{{ pairState(pair) }}</span></td>
                <td><t-button theme="primary" variant="text" size="small" @click="openPair(pair)">查看关联关系</t-button></td>
              </tr>
            </tbody>
          </table>
        </div>
        <div v-else class="empty-hint association-empty">
          <strong>暂无数据源关联</strong>
          <p>{{ crossSources.length < 2 ? '绑定至少两个已建模的数据源后，即可新增关联。' : '点击「新增关联」选择两个数据源。' }}</p>
        </div>
      </section>
    </template>
    <div v-else class="empty-hint">请选择或新建业务域。</div>
    <t-drawer :close-btn="true" :visible="pairDrawerVisible && !!selectedPair" :header="`${selectedPair?.left.name||''} ↔ ${selectedPair?.right.name||''} · 关联关系`" :footer="false" size="min(1040px, 96vw)" :close-on-esc-keydown="true" @close="pairDrawerVisible=false">
      <div v-if="selectedPair" class="drawer-body management-controls">
        <t-alert v-if="error" theme="error" :message="names.text(error)" />
        <div class="drawer-toolbar"><t-button theme="primary" :disabled="saving||!terminalJob(selectedPair.job)" @click="openRelation()">添加关联关系</t-button><t-button variant="outline" :loading="startingPair===selectedPair.key" :disabled="!canAnalyze(selectedPair)" @click="requestAnalysis(selectedPair)">更新关联关系</t-button><t-button v-if="selectedPair.job?.details?.checkpoint_ref && (selectedPair.job?.status === 'failed' || (selectedPair.job?.status === 'completed_with_pending' && selectedPair.job?.details?.technical_incomplete))" variant="outline" :loading="startingPair===selectedPair.key" :disabled="!canAnalyze(selectedPair)" @click="resumeCrossAnalysis(selectedPair)">继续上次分析</t-button><t-button v-if="!terminalJob(selectedPair.job)" theme="danger" :loading="cancellingPair===selectedPair.key||selectedPair.job.status==='cancelling'" :disabled="!canCancel(selectedPair)" @click="cancelCrossAnalysis(selectedPair)">{{selectedPair.job.phase==='publishing'?'正在发布结果':selectedPair.job.status==='cancelling'?'正在取消':'取消任务'}}</t-button></div>
        <div v-if="!terminalJob(selectedPair.job)" class="pair-job-info">
          <t-progress :percentage="selectedPair.job.progress || 0" size="small" />
          <p>{{ crossMessage(selectedPair.job) }}</p>
        </div>
        <t-alert v-else-if="selectedPair.job?.details?.technical_incomplete" theme="warning" message="部分范围未完成，原生效关系保留。" />
        <t-alert v-else-if="selectedPair.job?.error" theme="error" :message="names.text(selectedPair.job.error)" />
        <p v-else-if="selectedPair.job?.completed_at" class="pair-job-info">最近分析 <time :datetime="selectedPair.job.completed_at">{{ new Date(selectedPair.job.completed_at).toLocaleString() }}</time></p>
        <t-tabs v-model="relationFilter"><t-tab-panel value="all" :label="`全部 ${pairRows.length}`" /><t-tab-panel value="accepted" :label="`已生效 ${pairCounts.accepted||0}`" /><t-tab-panel value="pending" :label="`待审核 ${pairCounts.pending||0}`" /><t-tab-panel value="disabled" :label="`已停用 ${pairCounts.disabled||0}`" /></t-tabs>
        <div class="relation-list"><RelationshipCard v-for="{relation,id,status,decision_reason} in pagedRelations" :key="id" :source-table="objectOf(relation.from)?.table||names.display(relation.from)" :target-table="objectOf(relation.to)?.table||names.display(relation.to)" :source-fields="relationColumns(relation,'source')" :target-fields="relationColumns(relation,'target')" :source-label="sourceLabel(relation.from)" :target-label="sourceLabel(relation.to)" :cardinality="relation.cardinality" :status="status" :reason="names.text(decision_reason)" :condition="names.display(relation.condition)" :optional="relation.optional" :busy="saving||!terminalJob(selectedPair.job)" @edit="editReviewRelation({id,status,relation})" @decision="action=>action==='disable'?deletingRelation=relation:reviewRelation({id,status,relation},action)" /><div v-if="!visibleRelations.length" class="empty-list">{{ pairRows.length ? '当前状态下没有关联关系。' : '暂无表间关联，可点击「添加关联关系」手动添加。' }}</div></div>
        <t-pagination v-if="visibleRelations.length > 20" v-model="relationPage" :total="visibleRelations.length" :page-size="20" />
      </div>
    </t-drawer>
    <t-dialog :visible="!!analysisConfirmation" header="确认重新分析关联关系" :confirm-btn="{content:'确认分析',loading:!!startingPair}" :cancel-btn="{disabled:!!startingPair}" :close-btn="!startingPair" :close-on-overlay-click="!startingPair" :close-on-esc-keydown="!startingPair" @confirm="analyzeCrossSource(analysisConfirmation.left.id,analysisConfirmation.right.id)" @close="analysisConfirmation=null" @cancel="analysisConfirmation=null">
      <p>将重新分析“{{analysisConfirmation?.left.name}}”与“{{analysisConfirmation?.right.name}}”之间的关联。分析可能需要几分钟并消耗模型额度。</p><p>分析期间可取消；原有已生效关系保持可用，完整分析成功后才更新结果。</p><t-alert v-if="error" theme="error" :message="error" />
    </t-dialog>
    <t-dialog v-model:visible="pairPickerVisible" header="新增库间关联" :footer="false" width="520px" :close-on-overlay-click="!startingPair" :close-btn="!startingPair">
      <div class="domain-form management-controls"><p>选择当前业务域已绑定的两个数据源进行关联分析，结果在对应卡片中查看。</p><t-alert v-if="error" theme="error" :message="error" />
        <label>数据源 A<t-select v-model="crossLeft" filterable :disabled="!!startingPair" :options="pairLeftOptions.map(source=>({value:source.id,label:source.name}))" @change="crossRight = ''" /></label>
        <label>数据源 B<t-select v-model="crossRight" filterable :disabled="!!startingPair || !crossLeft" :options="pairRightOptions.map(source=>({value:source.id,label:source.name}))" /></label>
        <div class="dialog-actions"><t-button :disabled="!!startingPair" @click="pairPickerVisible=false">取消</t-button><t-button theme="primary" :loading="!!startingPair" :disabled="!canAddPair" @click="analyzeCrossSource()">开始分析</t-button></div>
      </div>
    </t-dialog>
    <t-dialog :visible="!!unbindingSource" header="解除数据源绑定" :confirm-btn="{content:'解除绑定',theme:'danger',loading:saving}" :cancel-btn="{disabled:saving}" :close-btn="!saving" :close-on-overlay-click="!saving" @confirm="unbindSource" @close="unbindingSource=null" @cancel="unbindingSource=null">
      <p>解除“{{unbindingSource?.name}}”与当前业务域的绑定，并移除本域引用它的表和跨库关系。数据源本身及其他业务域的绑定保留。</p><t-alert v-if="error" theme="error" :message="error" />
    </t-dialog>
    <t-dialog v-model:visible="sourcePickerVisible" header="添加数据源" :footer="false" width="520px" :close-on-overlay-click="!saving" :close-btn="!saving" :close-on-esc-keydown="!saving">
      <div class="domain-form management-controls">
        <t-alert v-if="error" theme="error" :message="error" />
        <label v-if="availableSources.length">选择数据源<t-select v-model="sourceToAdd" filterable :disabled="saving" placeholder="选择要添加的数据源" :options="availableSources.map(source=>({value:source.id,label:`${source.name} · ${source.type}`}))" /></label>
        <small>{{ availableSources.length ? '添加后将引用该数据源的表结构和关联关系。' : '暂无可添加的数据源。请先在数据源页面完成关联分析，再返回添加。' }}</small>
        <div class="dialog-actions"><t-button :disabled="saving" @click="sourcePickerVisible=false">取消</t-button><t-button theme="primary" :loading="saving" :disabled="!canBindSource||saving" @click="bindSource(sourceToAdd)">添加到当前业务域</t-button></div>
      </div>
    </t-dialog>
    <DeleteResourceDialog kind="业务域" :name="deletingDomain?.name || ''" :busy="deletionBusy"
                          :error="deletionError" @confirm="deleteDomain" @cancel="deletingDomain = null" />
    <t-dialog v-model:visible="createVisible" header="新建业务域" :footer="false" width="520px">
      <div class="domain-form management-controls">
        <label>业务域名称<t-input v-model="newDomain.name" placeholder="填写业务域名称" :maxlength="200" /></label>
        <label>业务域标识<t-input v-model="newDomain.domain" placeholder="例如：operations_analysis" :maxlength="100" /><small>2～100位小写字母、数字或下划线，以字母开头；创建后不可更改。</small></label>
        <t-alert v-if="createError" theme="error" :message="createError" />
        <div class="dialog-actions"><t-button variant="outline" @click="createVisible = false">取消</t-button><t-button theme="primary" :loading="creating" :disabled="!newDomain.name.trim() || !newDomain.domain.trim()" @click="createDomain">创建业务域</t-button></div>
      </div>
    </t-dialog>
    <t-dialog v-model:visible="relationVisible" :header="!editingDomainRelationId && relationIndex < 0 ? '添加关联关系' : '编辑关联关系'" width="min(800px, 94vw)" :footer="false" :close-on-overlay-click="!saving" :close-btn="!saving" :close-on-esc-keydown="!saving">
      <div class="relation-form management-controls">
        <t-alert v-if="relationError" theme="error" :message="relationError" />
        <div class="form-grid">
          <label>来源表<t-select v-model="relationDraft.from" :disabled="saving" filterable placeholder="选择来源表" @change="resetRelationFields"><t-option v-for="object in pairObjects" :key="object.name" :value="object.name" :label="`${tableLabel(object.name)} · ${sourceLabel(object.name)}`" /></t-select></label>
          <label>目标表<t-select v-model="relationDraft.to" :disabled="saving" filterable placeholder="选择目标表" @change="resetRelationFields"><t-option v-for="object in pairObjects.filter((o:any)=>o.dataSourceId!==sourceOf(relationDraft.from))" :key="object.name" :value="object.name" :label="`${tableLabel(object.name)} · ${sourceLabel(object.name)}`" /></t-select></label>
          <label>关联基数<t-select v-model="relationDraft.cardinality" :disabled="saving"><t-option value="1:1" label="一对一 (1:1)" /><t-option value="1:N" label="一对多 (1:N)" /><t-option value="N:1" label="多对一 (N:1)" /><t-option value="N:N" label="多对多 (N:N)" /></t-select></label>
          <label>可选关联<t-switch v-model="relationDraft.optional" :disabled="saving" /><small>允许来源表记录没有匹配的目标记录。</small></label>
        </div>
        <template v-if="pairMappingMode">
          <div><strong>字段映射</strong><p class="pair-description">每行选择一对字段，保存时自动生成关联条件；多行表示同时满足。</p></div>
          <div v-for="(pair,index) in relationPairs" :key="index" class="field-mapping-row"><label>来源字段<t-select v-model="pair.source" filterable :disabled="saving" :options="(objectOf(relationDraft.from)?.properties||[]).map((p:any)=>({label:p.column||p.label||p.name,value:p.name}))" /></label><span>=</span><label>目标字段<t-select v-model="pair.target" filterable :disabled="saving" :options="(objectOf(relationDraft.to)?.properties||[]).map((p:any)=>({label:p.column||p.label||p.name,value:p.name}))" /></label><t-button variant="text" theme="danger" :disabled="saving||relationPairs.length===1" @click="relationPairs.splice(index,1)">移除</t-button></div>
          <t-button variant="outline" :disabled="saving" @click="relationPairs.push({source:'',target:''})">添加一对字段</t-button>
          <div v-if="conditionPreview" class="generated-condition"><small>自动生成的关联条件</small><code>{{conditionPreview}}</code></div>
          <t-button variant="text" @click="advancedConditions=!advancedConditions">{{advancedConditions?'收起附加条件':'附加条件（可选）'}}</t-button>
          <label v-if="advancedConditions">附加条件<t-textarea v-model="extraCondition" :disabled="saving" :autosize="{minRows:2,maxRows:6}" /><small>仅在字段映射之外需要额外限定时填写，保存时会与字段映射一起生效。</small></label>
        </template>
        <label v-else>高级关联条件<t-textarea v-model="relationDraft.condition" :disabled="saving" :autosize="{ minRows: 3, maxRows: 8 }" /><small>此关系包含额外条件，编辑时保留完整表达式。</small></label>
        <div class="dialog-actions"><t-button variant="outline" :disabled="saving" @click="relationVisible = false">取消</t-button><t-button theme="primary" :loading="saving" @click="saveRelation">保存并生效</t-button></div>
      </div>
    </t-dialog>
    <t-dialog :visible="!!deletingRelation" header="停用关联关系" :confirm-btn="{ content: '确认停用', theme: 'danger', loading: saving }" :cancel-btn="{ disabled: saving }" :close-btn="!saving" :close-on-overlay-click="false" :close-on-esc-keydown="!saving" @confirm="deleteRelation" @cancel="deletingRelation = null" @close="deletingRelation = null">
      <p>确认停用“{{ deletingRelation?.label || deletingRelation?.name }}”？新查询不再使用该关联，可在“已停用”中重新启用。</p>
    </t-dialog>
  </div>
</template>

<style scoped>
.drawer-body{padding:4px 8px 20px}.drawer-toolbar{display:flex;gap:10px;margin-bottom:18px;flex-wrap:wrap}.drawer-body .relation-list{display:flex;flex-direction:column;margin:0 0 18px;padding:0}.empty-list{text-align:center;color:var(--semora-muted);padding:55px 20px}
.field-mapping-row{display:grid;grid-template-columns:minmax(0,1fr) 20px minmax(0,1fr) auto;align-items:end;gap:12px}.field-mapping-row>span{align-self:center;text-align:center}.generated-condition{padding:12px;border:1px solid var(--semora-line);border-radius:8px;background:var(--semora-page)}.generated-condition small{display:block;margin-bottom:8px}.generated-condition code{font-size:12px;overflow-wrap:anywhere}
.pair-drawer-toolbar{display:flex;justify-content:space-between;align-items:center;gap:14px;flex-wrap:wrap;margin-bottom:20px}.pair-drawer-toolbar p{font-size:13px;color:var(--semora-muted)}.pair-drawer-toolbar>div{display:flex;gap:10px}
.section-heading{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:12px 0;flex-wrap:wrap}.section-heading .panel-title{padding:0;border:0;font-size:16px}
.header-actions{display:flex;gap:10px;align-items:center}.domain-summary{display:flex;justify-content:space-between;align-items:center;gap:20px;margin-bottom:22px;background:#edf4fb;border-radius:6px;padding:12px 16px}.domain-heading{display:flex;align-items:center;gap:12px}.domain-heading strong{font-size:18px}.domain-heading small{display:block;color:var(--semora-muted);margin-top:6px}.ontology-stats{display:flex;gap:18px;font-size:13px}.ontology-stats span{color:var(--semora-muted)}.ontology-stats b{color:var(--semora-ink);margin-left:8px}.source-package-panel{margin-bottom:20px;overflow:hidden}.package-list{display:grid}.cross-analysis small,.cross-candidates small{display:block;color:var(--semora-muted);margin-top:5px}.cross-analysis{display:flex;gap:12px;align-items:center;padding:18px;border-top:1px solid var(--semora-line);background:var(--semora-page)}.cross-analysis>div{flex:1}.cross-candidates{display:grid;gap:1px;background:var(--semora-line)}.cross-candidates article{display:flex;align-items:center;gap:8px;padding:12px 18px;background:var(--semora-surface)}.cross-candidates article>div{flex:1}.model-panel{overflow:hidden;margin-bottom:20px}.relation-list{display:grid;gap:16px;padding:18px}.relation-header{display:flex;justify-content:space-between;align-items:center;gap:12px}.relation-header small{display:block;margin-top:5px;color:var(--semora-muted);overflow-wrap:anywhere}.relation-actions{display:flex;flex:none}.relation-path{display:grid;grid-template-columns:minmax(0,1fr) 120px minmax(0,1fr);align-items:center;gap:16px;margin:18px 0}.table-node{background:var(--semora-page);border:1px solid var(--semora-line);border-radius:8px;padding:14px;overflow-wrap:anywhere}.table-node small{display:block;color:var(--semora-muted);margin-top:8px}.relation-link{display:flex;flex-direction:column;align-items:center;gap:6px;color:var(--semora-green)}.relation-link small{color:var(--semora-muted)}.relation-condition{display:flex;gap:10px;align-items:flex-start;flex-wrap:wrap;font-size:12px}.relation-condition>span{color:var(--semora-muted)}.relation-condition code{overflow-wrap:anywhere;flex:1;min-width:0}.domain-form,.relation-form{display:flex;flex-direction:column;gap:20px;padding-top:6px}.domain-form label,.relation-form label{display:flex;flex-direction:column;gap:8px;min-width:0}.domain-form small,.relation-form small{color:var(--semora-muted);line-height:1.6}.form-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:18px}.dialog-actions{display:flex;justify-content:flex-end;gap:10px}.empty-hint{padding:30px;text-align:center;color:var(--semora-muted)}
@media(max-width:800px){.header-actions,.cross-analysis{flex-wrap:wrap}.page-header{flex-wrap:wrap;gap:16px}.relation-path{grid-template-columns:1fr}.relation-link svg{transform:rotate(90deg)}.form-grid{grid-template-columns:1fr}.relation-header{align-items:flex-start}}
.package-source { display: flex; align-items: center; gap: 10px; min-width: 0; overflow-wrap: anywhere; }
.ontology-page {
  max-width: 1540px;
  padding-inline: clamp(24px, 2.5vw, 40px);
  font-family: Arial, "PingFang SC", "Microsoft YaHei", sans-serif;
  --management-blue: #0c52a4;
}
.ontology-page > .page-header { margin-bottom: 30px; }
.ontology-page > .page-header h1 { margin: 0; font-size: 30px; }
.ontology-page :deep(.t-button) { font-family: inherit; }
.domain-bar { display: flex; align-items: center; gap: 16px; border-bottom: 1px solid var(--semora-line); margin-bottom: 32px; }
.domain-switcher { display: flex; flex: 1; gap: 22px; overflow-x: auto; }
.domain-switcher button { border: 0; border-bottom: 2px solid transparent; background: transparent; color: var(--semora-muted); padding: 10px 10px 12px; cursor: pointer; white-space: nowrap; font-size: 16px; }
.domain-switcher button[aria-current] { border-color: var(--management-blue); color: var(--management-blue); font-weight: 650; }
.domain-switcher button:hover { color: var(--management-blue); }
.domain-switcher button:disabled { cursor: not-allowed; }
.domain-heading { font-size: 15px; color: #415268; overflow-wrap: anywhere; }
.domain-heading strong { font-size: 16px; color: var(--semora-ink); margin-left: 7px; }
.domain-summary { background: #e8eef5; justify-content: flex-start; padding: 13px 22px; min-height: 62px; margin-bottom: 27px; flex-wrap: wrap; }
.ontology-stats { gap: 22px; font-size: 15px; flex-wrap: wrap; }
.ontology-stats > span { border-inline-start: 1px solid #bac6d4; padding-inline-start: 22px; }
.section-heading .panel-title { margin: 0; font-size: 22px; }
.source-package-panel { margin-bottom: 28px; }
.ontology-table-wrap { overflow-x: auto; border-radius: 6px 6px 0 0; }
.ontology-table { width: 100%; border-collapse: collapse; font-size: 15px; min-width: 580px; }
.ontology-table :is(th, td) { padding: 12px 20px; text-align: left; overflow-wrap: anywhere; }
.ontology-table thead th { background: #e6ebf0; color: #253244; font-size: 15px; font-weight: 650; white-space: nowrap; padding-block: 11px; }
.ontology-table tbody tr { border-bottom: 1px solid var(--semora-line); }
.ontology-table tbody tr:hover { background: #f7faff; }
.ontology-table tbody td { font-variant-numeric: tabular-nums; }
.ontology-table :is(th, td):last-child { width: 14%; white-space: nowrap; }
.source-table th:first-child { width: 33%; }
.source-table th:nth-child(2), .source-table th:nth-child(3) { width: 18%; }
.ontology-table :deep(.t-button--variant-text) { padding-inline: 0; font-size: 15px; }
.table-status { display: inline-flex; align-items: center; gap: 8px; }
.table-status::before { content: ""; width: 8px; height: 8px; border-radius: 50%; background: #8c98a8; flex: none; }
.table-status.ready::before { background: #2d9c53; }
.table-status.pending::before { background: #e59427; }
.table-status.running::before { background: #256bc5; }
.table-status.failed::before { background: #c9353f; }
.source-table .database-icon { --database-icon-size: 28px; }
.association-table th:first-child { width: 27%; }
.association-table th:nth-child(n+2):nth-child(-n+4) { width: 15%; }
.pair-endpoints { display: flex; align-items: center; flex-wrap: wrap; gap: 4px 10px; }
.pair-endpoints > span { color: var(--semora-muted); }
.pair-job-info { margin: 0 0 12px; color: var(--semora-muted); font-size: 13px; }
.pair-job-info p { margin: 6px 0 0; }
.association-empty { border-top: 1px solid var(--semora-line); text-align: left; padding: 18px 0; font-size: 13px; }
.association-hint { margin: 5px 0 0; color: var(--semora-muted); font-size: 13px; line-height: 1.6; }
.association-empty strong { color: var(--semora-ink); }
.association-empty p { margin: 4px 0 0; }
</style>
