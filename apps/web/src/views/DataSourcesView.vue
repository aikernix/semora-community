<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { DataBaseIcon, LinkIcon, RefreshIcon, SearchIcon } from 'tdesign-icons-vue-next'
import { api } from '../api'
import DeleteResourceDialog from '../components/DeleteResourceDialog.vue'
import { hasRole } from '../auth'
import DatabaseIcon from '../components/DatabaseIcon.vue'
import RelationshipCard from '../components/RelationshipCard.vue'
import DataSourceCreateDialog from '../components/DataSourceCreateDialog.vue'

interface DataSource { id:string; name:string; type:string; status:string; access:string; objects:number; connection?:Record<string,any>; onboarding?:Record<string,any>; relationshipJob?:Record<string,any>; last_scan_at?:string }
const emptyForm=()=>({name:'',type:'postgresql',host:'',port:5432,database:'',username:'',password:'',schema_name:'',catalog:'',compatibility_mode:'',service_kind:'service',jdbc_url:'',driver_id:'',product_override:''})
const sources=ref<DataSource[]>([]),products=ref<any[]>([]),drivers=ref<any[]>([])
const selectedId=ref(''),sourceSearch=ref(''),creating=ref(false),createVisible=ref(false)
const form=ref(emptyForm()),connectionLoading=ref(false)
const connectionState=ref<'idle'|'testing'|'success'|'error'>('idle'),connectionMessage=ref(''),detectedVersion=ref(''),detectedProduct=ref(''),pageError=ref('')
const canManage=computed(()=>hasRole('data_steward','admin'));let poller:ReturnType<typeof setTimeout>|undefined;let polling=false,disposed=false
const deletingSource = ref<DataSource | null>(null)
const deletionBusy = ref(false)
const deletionError = ref('')
function openDeleteSource(item: DataSource) {
  deletionError.value = ''
  deletingSource.value = item
}
async function deleteSource() {
  const item = deletingSource.value
  if (!item || deletionBusy.value || !hasRole('admin')) return
  deletionBusy.value = true
  deletionError.value = ''
  try {
    await api.delete(`/datasources/${encodeURIComponent(item.id)}`)
  } catch (cause: any) {
    deletionError.value = cause.response?.data?.detail || '删除数据源失败'
    return
  } finally { deletionBusy.value = false }
  deletingSource.value = null
  sources.value = sources.value.filter(source => source.id !== item.id)
  if (selectedId.value === item.id) {
    selectedId.value = ''
    showTables.value = false
    showRelations.value = false
    if (sources.value.length) await selectSource(sources.value[0])
  }
}
const selected=computed(()=>sources.value.find(item=>item.id===selectedId.value)||null)
const filteredSources=computed(()=>{const q=sourceSearch.value.trim().toLowerCase();return q?sources.value.filter(item=>`${item.name} ${item.type}`.toLowerCase().includes(q)):sources.value})
function isTableScan(job?: Record<string, any> | null) {
  return job?.details?.metadata_only === true || job?.phase === 'metadata_complete'
}
const latestJob = computed(() => selected.value?.onboarding || null)
const activeOnboarding = computed(() => isTableScan(latestJob.value) ? selected.value?.relationshipJob || null : latestJob.value)
const scanJob = computed(() => isTableScan(latestJob.value) ? latestJob.value : null)
const analysisRunning = computed(() => isModelingActive(activeOnboarding.value))
const scanOutcome = computed(() => {
  const job = scanJob.value
  if (!job || isModelingActive(job)) return null
  if (job.status === 'completed') return { success: true, message: '扫描表信息成功' }
  const detail = job.status === 'cancelled' ? '扫描已取消' : job.error
  return { success: false, message: detail ? `扫描表信息失败：${detail}` : '扫描表信息失败' }
})
const terminalStatuses=new Set(['completed','completed_with_pending','failed','cancelled'])
function isModelingActive(job?:Record<string,any>|null){return !!job&&!terminalStatuses.has(job.status)}
const modelingBusy=computed(()=>isModelingActive(latestJob.value))
const modelingAction=ref('')
const canCancelModeling=computed(()=>analysisRunning.value&&activeOnboarding.value?.phase!=='publishing'&&activeOnboarding.value?.status!=='cancelling')
function phaseLabel(phase?:string){return({queued:'等待开始',testing:'验证连接',probing:'探测数据库能力',scanning:'扫描表结构',discovering:'发现关联关系',profiling:'核验关系证据',understanding_tables:'理解表与字段职责',researching:'验证关系依据',reading:'提取搜索线索',extracting_keywords:'提取全库关键词',organizing_recall_groups:'整理关键词召回组',organization_complete:'召回组整理完成',analyzing_large_groups:'自主分析大命中组',keywords_complete:'关键词提取完成',retrieval_complete:'关键词检索完成',discovery_complete:'关系发现完成',interrupted:'阶段任务已中断',comparing:'分批分析关联',expanding:'补充搜索线索',checking_references:'核查引用完整性',auditing_evidence:'复核新增证据',auditing_domains:'核查编号体系',cancelled:'已取消',cancelling:'正在取消',publishing:'正在发布关系',building:'生成单源本体',using_persisted_metadata:'读取已保存结构',complete:'关联关系已更新',complete_with_pending:'关联分析已完成，有待审核',failed:'处理失败',incomplete:'部分分析未完成，原生效关系保留',resuming:'恢复任务'} as Record<string,string>)[phase||'']||phase||'尚未开始'}
function statusLabel(status:string){return({healthy:'连接正常',unsafe:'账号权限过高',unverified:'等待验证',scan_only:'仅支持扫描'} as Record<string,string>)[status]||status}
function statusTheme(status:string){return status==='healthy'?'success':status==='unsafe'?'danger':'warning'}
async function loadSources(first=false,checkJobs=true){
  const items=(await api.get('/datasources')).data as DataSource[]
  await Promise.all(items.map(async item=>{
    const previous = sources.value.find(source => source.id === item.id)
    if (!checkJobs) {
      item.onboarding = previous?.onboarding
      item.relationshipJob = previous?.relationshipJob
      return
    }
    try {
      item.onboarding = (await api.get(`/datasources/${item.id}/onboarding`)).data
    } catch (cause: any) {
      if (cause.response?.status !== 404) throw cause
    }
    if (isTableScan(item.onboarding)) {
      try {
        const job = (await api.get(`/datasources/${item.id}/onboarding`, { params: { relationships_only: true } })).data
        if (!isTableScan(job)) item.relationshipJob = job
      } catch (cause: any) {
        if (cause.response?.status !== 404) throw cause
      }
    }
  }))
  if(disposed)return
  sources.value=items
  if(first&&!selectedId.value&&items.length)await selectSource(items[0])
}
function schedulePoll(){
  if(poller)clearTimeout(poller)
  poller=undefined
  if(disposed||polling||!sources.value.some(s=>isModelingActive(s.onboarding)))return
  poller=setTimeout(()=>void pollActiveJobs(),3000)
}
async function pollActiveJobs(){
  if(disposed||polling)return
  polling=true
  const active=sources.value.filter(s=>isModelingActive(s.onboarding)).map(s=>({id:s.id,job:s.onboarding?.id}))
  let finished=false
  try{
    await Promise.all(active.map(async watched=>{
      try{
        const job=(await api.get(`/datasources/${watched.id}/onboarding`)).data
        const item=sources.value.find(s=>s.id===watched.id)
        if(disposed||!item||item.onboarding?.id!==watched.job)return
        item.onboarding=job;finished ||= !isModelingActive(job)
      }catch(cause:any){
        if(disposed)return
        const item=sources.value.find(s=>s.id===watched.id)
        if(cause.response?.status===404){if(item&&item.onboarding?.id===watched.job)item.onboarding=undefined}
        else pageError.value=cause.response?.data?.detail||'无法更新后台任务状态，将自动重试'
      }
    }))
    if(finished&&!disposed)await loadSources(false,false)
  }catch(cause:any){if(!disposed)pageError.value=cause.response?.data?.detail||'无法刷新数据源'}
  finally{polling=false;schedulePoll()}
}
watch(()=>sources.value.filter(s=>isModelingActive(s.onboarding)).map(s=>`${s.id}:${s.onboarding?.id}`).join('|'),schedulePoll)

async function selectSource(item:DataSource){pageError.value='';selectedId.value=item.id;connectionLoading.value=true;form.value={...emptyForm(),name:item.name,type:item.type};connectionState.value='idle';connectionMessage.value='';try{const connection=(await api.get(`/datasources/${item.id}/connection`)).data;if(selectedId.value!==item.id)return;form.value={...emptyForm(),...connection,name:item.name,type:item.type,password:''};detectedVersion.value=item.connection?.version||'';detectedProduct.value=item.connection?.product||''}catch(cause:any){if(selectedId.value===item.id)pageError.value=cause.response?.data?.detail||'无法读取数据源连接配置'}finally{if(selectedId.value===item.id)connectionLoading.value=false}}
async function sourceCreated(created:any){try{await loadSources();const item=sources.value.find(source=>source.id===created.id);if(item)await selectSource(item)}catch(cause:any){pageError.value=cause.response?.data?.detail||'无法刷新数据源'}}
async function testConnection(){if(!selected.value||connectionLoading.value||connectionState.value==='testing')return;const sourceId=selected.value.id;connectionState.value='testing';connectionMessage.value='';pageError.value='';try{const result=(await api.post(`/datasources/${sourceId}/test`)).data;if(selectedId.value!==sourceId)return;detectedVersion.value=result.version||detectedVersion.value;detectedProduct.value=result.product||detectedProduct.value;connectionMessage.value=`连接成功${result.version?` · ${result.version}`:''}`;await loadSources();if(selectedId.value===sourceId)connectionState.value='success'}catch(cause:any){if(selectedId.value!==sourceId)return;connectionState.value='error';connectionMessage.value=cause.response?.data?.detail||cause.message||'连接失败'}}
async function saveSource(){if(!selected.value||connectionLoading.value||creating.value)return;creating.value=true;pageError.value='';try{await api.patch(`/datasources/${selected.value.id}`,{name:form.value.name});await loadSources()}catch(cause:any){pageError.value=cause.response?.data?.detail||cause.message||'保存失败'}finally{creating.value=false}}
async function modelingOperation(action:'scan'|'analyze'|'cancel'|'resume'){
  const item=selected.value;if(!item||modelingAction.value)return
  const job=action==='resume'||action==='cancel'?activeOnboarding.value:item.onboarding
  modelingAction.value=action;pageError.value=''
  try{
    const path=action==='resume'?`/datasources/${item.id}/onboarding/${job?.id}/resume`:action==='cancel'?`/datasources/${item.id}/onboarding/${job?.id}/cancel`:action==='analyze'?`/datasources/${item.id}/relationships/analyze`:`/datasources/${item.id}/rescan`
    const response = (await api.post(path)).data
    if (action === 'scan' && !isTableScan(item.onboarding)) item.relationshipJob = item.onboarding
    item.onboarding = response
    await loadSources()
  }catch(cause:any){pageError.value=cause.response?.data?.detail||(action==='scan'?'扫描表信息失败':'任务操作失败，请重试')}finally{modelingAction.value=''}
}
async function resumeAnalysis(){await modelingOperation('resume')}
async function rescan(){await modelingOperation('scan')}
const analysisConfirmation=ref<DataSource|null>(null)
function analyzeRelations(){if(selected.value&&!modelingBusy.value){pageError.value='';analysisConfirmation.value=selected.value}}
async function confirmAnalysis(){if(analysisConfirmation.value?.id!==selected.value?.id)return;await modelingOperation('analyze');if(!pageError.value)analysisConfirmation.value=null}
async function cancelModeling(){await modelingOperation('cancel')}

const showTables=ref(false),tableQuery=ref(''),tablePage=ref(1),tablePageSize=ref(20),tableTotal=ref(0),tableItems=ref<any[]>([]),tableDetail=ref<any>(null),drawerLoading=ref(false)
async function loadTables(reset=false){if(!selected.value)return;if(reset)tablePage.value=1;drawerLoading.value=true;tableDetail.value=null;try{const result=(await api.get(`/datasources/${selected.value.id}/tables`,{params:{page:tablePage.value,page_size:tablePageSize.value,query:tableQuery.value}})).data;tableItems.value=result.items;tableTotal.value=result.total}finally{drawerLoading.value=false}}
async function openTables(){showTables.value=true;tableQuery.value='';await loadTables(true)}
async function openTableDetail(table:any){if(!selected.value)return;drawerLoading.value=true;try{tableDetail.value=(await api.get(`/datasources/${selected.value.id}/tables/detail`,{params:{table_id:table.id}})).data}finally{drawerLoading.value=false}}
function changeTablePage(value:number){tablePage.value=value;void loadTables()}
function changeTablePageSize(value:number){tablePageSize.value=value;void loadTables(true)}

const showRelations=ref(false),relationItems=ref<any[]>([]),relationPage=ref(1),relationTotal=ref(0),relationEditing=ref(false),relationBusy=ref(false),relationError=ref('')
const relationStatus=ref<'all'|'accepted'|'pending'|'disabled'>('all')
const relationCounts=ref<Record<string,number>>({all:0,accepted:0,pending:0,disabled:0})
const relationForm=ref<any>({}),relationTables=ref<any[]>([]),relationColumns=ref<Record<string,any[]>>({source:[],target:[]})
const knownCardinalities=['1:1','1:N','N:1','N:N']
async function loadRelations(){if(!selected.value)return;const r=(await api.get(`/datasources/${selected.value.id}/relationships`,{params:{status:relationStatus.value,page:relationPage.value,page_size:20}})).data;relationItems.value=r.items;relationTotal.value=r.total;relationCounts.value=r.counts||{all:0,accepted:0,pending:0,disabled:0}}
async function changeRelationStatus(value:string|number){relationStatus.value=value as typeof relationStatus.value;relationPage.value=1;await loadRelations()}
async function searchRelationTables(query=''){if(!selected.value)return;const r=(await api.get(`/datasources/${selected.value.id}/tables`,{params:{query,page:1,page_size:100}})).data;relationTables.value=r.items.map((t:any)=>({label:t.id,value:t.id}))}
async function openRelations(){showRelations.value=true;relationEditing.value=false;relationStatus.value='all';relationPage.value=1;relationError.value='';try{await loadRelations()}catch(e:any){relationError.value=e.response?.data?.detail||'无法读取关联关系'}}
async function relationSide(side:'source'|'target'){const id=relationForm.value[side+'_table_id'];relationColumns.value[side]=id?(await api.get(`/datasources/${selected.value?.id}/tables/detail`,{params:{table_id:id}})).data.columns:[]}
async function editSourceRelation(r?:any){relationError.value='';relationForm.value=r?JSON.parse(JSON.stringify(r)):{source_table_id:'',target_table_id:'',cardinality:'N:1',key_pairs:[{source:'',target:''}]};relationEditing.value=true;try{await searchRelationTables();for(const id of [r?.source_table_id,r?.target_table_id].filter(Boolean)){if(!relationTables.value.some(t=>t.value===id))relationTables.value.push({label:id,value:id})}await Promise.all([relationSide('source'),relationSide('target')])}catch(e:any){relationError.value=e.response?.data?.detail||'无法读取表字段'}}
async function saveSourceRelation(){if(!selected.value)return;relationBusy.value=true;relationError.value='';try{const f=relationForm.value;const body={source_table_id:f.source_table_id,target_table_id:f.target_table_id,key_pairs:f.key_pairs,cardinality:f.cardinality};if(f.id)await api.put(`/datasources/${selected.value.id}/relationships/${f.id}`,body);else await api.post(`/datasources/${selected.value.id}/relationships`,body);relationEditing.value=false;await loadRelations()}catch(e:any){relationError.value=e.response?.data?.detail||'保存关系失败'}finally{relationBusy.value=false}}
const disablingSourceRelation=ref<any>(null)
async function decideSourceRelation(action:'approve'|'reject'|'disable'|'enable',r?:any){const target=r||disablingSourceRelation.value;if(!selected.value||!target)return;relationBusy.value=true;relationError.value='';try{await api.post(`/datasources/${selected.value.id}/relationships/${target.id}/decision`,{action});disablingSourceRelation.value=null;relationPage.value=1;await loadRelations()}catch(e:any){relationError.value=e.response?.data?.detail||'关系状态更新失败'}finally{relationBusy.value=false}}
function relationStatusLabel(status:string){return({accepted:'已生效',pending:'待审核',disabled:'已停用'} as Record<string,string>)[status]||status}
function relationStatusTheme(status:string){return status==='accepted'?'success':status==='pending'?'warning':'default'}

const showDrivers=ref(false),editingDriverId=ref(''),driverFiles=ref<File[]>([]),driverConfig=ref(JSON.stringify({name:'',driver_class:'',version:'',enabled:false,dialect:'',readonly_strategy:'',pagination:'baseline',type_mapping:{},metadata_queries:{}},null,2))
const driverKeys=['name','driver_class','version','enabled','dialect','readonly_strategy','pagination','type_mapping','metadata_queries']
async function installDriver(){try{const value=JSON.parse(driverConfig.value);if(editingDriverId.value)await api.put(`/settings/database-drivers/${editingDriverId.value}`,value);else{const data=new FormData();data.append('config',driverConfig.value);driverFiles.value.forEach(file=>data.append('files',file));await api.post('/settings/database-drivers',data,{timeout:200000})}drivers.value=(await api.get('/settings/database-drivers')).data;editingDriverId.value=''}catch(cause:any){pageError.value=cause.response?.data?.detail||'驱动保存失败'}}
function editDriver(driver:any){editingDriverId.value=driver.id;driverConfig.value=JSON.stringify(Object.fromEntries(driverKeys.filter(key=>key in driver).map(key=>[key,driver[key]])),null,2)}
async function toggleDriver(driver:any){const value=Object.fromEntries(driverKeys.filter(key=>key in driver).map(key=>[key,driver[key]]));value.enabled=!driver.enabled;await api.put(`/settings/database-drivers/${driver.id}`,value);drivers.value=(await api.get('/settings/database-drivers')).data}
onMounted(async()=>{try{products.value=(await api.get('/datasource-products')).data;drivers.value=(await api.get('/settings/database-drivers')).data;await loadSources(true);schedulePoll()}catch(cause:any){pageError.value=cause.response?.data?.detail||'无法加载数据源'}})
onUnmounted(()=>{disposed=true;if(poller)clearTimeout(poller)})
</script>

<template>
  <div class="page management-page datasource-page">
    <div class="page-header"><div><h1>数据源</h1><p>维护只读数据库连接，查看已扫描的表结构与关联关系。</p></div><div class="header-actions"><t-button v-if="hasRole('admin')" variant="outline" @click="showDrivers=true">驱动管理</t-button><t-button v-if="canManage" theme="primary" @click="createVisible=true">添加数据源</t-button></div></div>
    <t-alert v-if="pageError" theme="error" :message="pageError" closable @close="pageError=''" />
    <div class="workspace">
      <aside class="source-sidebar"><div class="source-search"><t-input v-model="sourceSearch" clearable placeholder="搜索数据源"><template #prefix-icon><SearchIcon /></template></t-input></div><button v-for="item in filteredSources" :key="item.id" class="source-item" :class="{active:item.id===selectedId}" @click="selectSource(item)"><DatabaseIcon :type="item.type" /><span class="source-copy"><strong :title="item.name">{{ item.name }}</strong><small>{{ item.type }} · {{ item.objects }} 张表</small><small v-if="item.onboarding&&!isTableScan(item.onboarding)&&isModelingActive(item.onboarding)">{{ phaseLabel(item.onboarding.phase) }} {{ item.onboarding.progress }}%</small></span><i class="connection-dot" :class="item.status"></i></button><div v-if="!filteredSources.length" class="source-list-empty">
        <template v-if="sourceSearch.trim() && sources.length">
          <span>没有匹配的数据源</span>
          <t-button size="small" variant="text" theme="primary" @click="sourceSearch=''">清除搜索</t-button>
        </template>
        <span v-else>暂无数据源</span>
      </div></aside>
      <div class="source-form-panel">
        <template v-if="selected">
          <div class="form-heading"><div class="source-title"><DatabaseIcon :type="selected.type" branded /><div><div class="source-title-line"><h2>{{selected.name}}</h2><t-tag :theme="statusTheme(selected.status) as any" variant="light">{{statusLabel(selected.status)}}</t-tag></div><p class="source-subtitle">{{products.find(p=>p.id===selected?.type)?.label||selected.type}} · {{selected.objects}} 张表</p></div></div>
            <div class="resource-links">
            <t-button variant="outline" :disabled="selected.objects<1" @click="openTables"><template #icon><DataBaseIcon /></template>查看数据表</t-button>
            <t-button variant="outline" :disabled="selected.objects<1" @click="openRelations"><template #icon><LinkIcon /></template>查看关联关系</t-button>
              <t-button v-if="hasRole('admin')" theme="danger" variant="text" @click="openDeleteSource(selected)">删除数据源</t-button>
            </div>
          </div>
          <div class="source-overview">
            <section class="connection-panel detail-panel">
              <div class="detail-panel-heading"><h3>连接信息</h3><t-button variant="outline" :loading="connectionState==='testing'" @click="testConnection"><template #icon><LinkIcon/></template>测试连接</t-button></div>
              <dl class="connection-facts"><div><dt>数据库类型</dt><dd>{{products.find(p=>p.id===form.type)?.label||form.type}}</dd></div><div><dt>数据库版本</dt><dd>{{[detectedProduct,detectedVersion].filter(Boolean).join(' ')||'连接测试后自动识别'}}</dd></div><div><dt>数据库地址</dt><dd>{{form.host||'—'}}</dd></div><div><dt>端口</dt><dd>{{form.port||'—'}}</dd></div><div><dt>{{form.type==='oracle'?'服务名（Service Name）':'数据库名'}}</dt><dd>{{form.database||'—'}}</dd></div><div><dt>用户名</dt><dd>{{form.username||'—'}}</dd></div></dl>
              <details class="advanced"><summary>更多连接信息</summary><dl class="connection-facts"><div><dt>Schema</dt><dd>{{form.schema_name||'默认'}}</dd></div><div><dt>Catalog</dt><dd>{{form.catalog||'默认'}}</dd></div><div v-if="form.compatibility_mode"><dt>兼容模式</dt><dd>{{form.compatibility_mode}}</dd></div><div v-if="form.type==='jdbc'"><dt>JDBC 驱动</dt><dd>{{drivers.find(d=>d.id===form.driver_id)?.name||'默认驱动'}}</dd></div></dl></details>
              <details v-if="canManage" :key="selected.id" class="advanced rename-source">
                <summary>编辑名称</summary>
                <div class="source-name-row">
                  <label>数据源名称<t-input v-model="form.name" /></label>
                  <t-button theme="primary" :loading="creating" :disabled="connectionLoading || !form.name.trim() || form.name === selected.name" @click="saveSource">保存名称</t-button>
                </div>
              </details>
              <div v-if="connectionState!=='idle'" class="connection-result" :class="connectionState"><span class="result-dot"></span><strong>{{connectionState==='testing'?'正在测试连接':connectionMessage}}</strong></div>
            </section>
            <section class="task-panel detail-panel">
              <div class="detail-panel-heading">
                <h3>任务执行情况</h3>
                <t-tag variant="light" :theme="analysisRunning ? 'primary' : activeOnboarding?.status === 'failed' ? 'danger' : activeOnboarding?.details?.technical_incomplete ? 'warning' : 'default'">
                  {{ analysisRunning ? '进行中' : activeOnboarding?.status === 'cancelled' ? '已取消' : activeOnboarding?.status === 'failed' ? '失败' : activeOnboarding?.details?.technical_incomplete ? '部分未完成' : activeOnboarding ? '已结束' : '尚无关联分析' }}
                </t-tag>
              </div>
              <div v-if="activeOnboarding" class="task-state">
                <div v-if="activeOnboarding.status === 'cancelled'" class="task-stop">
                  <span v-if="activeOnboarding.progress != null">停止于 {{ activeOnboarding.progress }}%</span>
                  <span>已有生效关联保持可用。</span>
                </div>
                <template v-else>
                  <div class="task-phase">
                    <strong>{{ phaseLabel(activeOnboarding.phase) }}</strong>
                    <span v-if="analysisRunning">{{ activeOnboarding.progress || 0 }}%</span>
                  </div>
                  <t-progress v-if="analysisRunning" :percentage="activeOnboarding.progress || 0" :label="false" size="small" />
                  <p v-if="analysisRunning">{{ activeOnboarding.status === 'cancelling' ? '正在停止任务，请稍候。' : activeOnboarding.phase === 'publishing' ? '正在发布完整结果，请稍候。' : '后台处理中，已扫描的数据表仍可正常查看。' }}</p>
                </template>
                <p v-if="activeOnboarding.error" class="error-text">{{ activeOnboarding.error }}</p>
              </div>
              <dl class="task-facts">
                <div><dt>已扫描数据表</dt><dd>{{ selected.objects }}</dd></div>
                <div><dt>最近扫描</dt><dd>{{ selected.last_scan_at ? new Date(selected.last_scan_at).toLocaleString() : '暂无记录' }}</dd></div>
              </dl>
              <div v-if="canManage" class="task-actions"><t-button variant="outline" :disabled="modelingBusy||!!modelingAction" @click="rescan"><template #icon><RefreshIcon/></template>重新扫描表信息</t-button><t-button variant="outline" :loading="modelingAction==='analyze'" :disabled="modelingBusy||!!modelingAction" @click="analyzeRelations">重新分析关联关系</t-button><t-button v-if="(activeOnboarding?.status==='failed'||activeOnboarding?.details?.technical_incomplete)&&activeOnboarding?.details?.checkpoint_ref" variant="outline" :loading="modelingAction==='resume'" :disabled="modelingBusy||!!modelingAction" @click="resumeAnalysis">继续上次分析</t-button><t-button v-if="analysisRunning" theme="danger" variant="outline" :loading="modelingAction==='cancel'||activeOnboarding?.status==='cancelling'" :disabled="!canCancelModeling||!!modelingAction" @click="cancelModeling">取消任务</t-button></div>
              <p v-if="scanOutcome" class="scan-outcome" :class="{ 'error-text': !scanOutcome.success }" role="status">{{ scanOutcome.message }}</p>
            </section>
          </div>
        </template><div v-else class="blank-state"><DataBaseIcon size="48px"/><h3>选择一个数据源</h3><p>从左侧选择数据源查看详情，或点击右上角添加数据源。</p></div>
      </div>
    </div>

    <DeleteResourceDialog kind="数据源" :name="deletingSource?.name || ''" :busy="deletionBusy"
                          :error="deletionError" @confirm="deleteSource" @cancel="deletingSource = null" />
    <DataSourceCreateDialog v-model:visible="createVisible" :products="products" :drivers="drivers" @created="sourceCreated" />

    <t-drawer :close-btn="true" :close-on-esc-keydown="true" :visible="showTables" :header="tableDetail?'表结构详情':`${selected?.name||''} · 数据表`" :footer="false" size="min(1040px, 96vw)" @close="showTables=false"><div v-if="!tableDetail" class="drawer-body management-controls"><div class="drawer-toolbar"><t-input v-model="tableQuery" clearable placeholder="搜索表名、Schema 或说明" @enter="loadTables(true)"><template #prefix-icon><SearchIcon /></template></t-input><t-button @click="loadTables(true)">搜索</t-button></div><div class="data-table"><div class="table-row table-head"><span>表名</span><span>类型</span><span>说明</span><span>字段数</span><span></span></div><div v-for="table in tableItems" :key="table.id" class="table-row"><span><strong>{{ [table.schema,table.name].filter(Boolean).join('.') }}</strong><small>{{ table.primary_keys?.length?`主键：${table.primary_keys.flatMap((key:any)=>key.columns||[]).join(', ')}`:'未声明主键' }}</small></span><span>{{ table.kind||'TABLE' }}</span><span>{{ table.description||'—' }}</span><span>{{ table.column_count }}</span><span><t-button variant="text" @click="openTableDetail(table)">查看 Schema</t-button></span></div></div><t-pagination :current="tablePage" :page-size="tablePageSize" :total="tableTotal" :page-size-options="[10,20,50,100]" @current-change="changeTablePage" @page-size-change="changeTablePageSize" /></div><div v-else class="schema-detail management-controls"><t-button variant="text" @click="tableDetail=null">← 返回表目录</t-button><div class="schema-title"><h3>{{ [tableDetail.schema,tableDetail.name].filter(Boolean).join('.') }}</h3><p>{{ tableDetail.description||'暂无表说明' }}</p></div><div class="column-table"><div class="column-head"><span>字段</span><span>类型</span><span>可空</span><span>说明</span></div><div v-for="column in tableDetail.columns||[]" :key="column.name" class="column-line"><code>{{ column.name }}</code><span>{{ column.raw_type||column.type }}</span><span>{{ column.nullable?'是':'否' }}</span><span>{{ column.description||column.comment||'—' }}</span></div></div><div class="constraint-grid"><article><h4>主键</h4><pre>{{ JSON.stringify(tableDetail.primary_keys||[],null,2) }}</pre></article><article><h4>唯一约束</h4><pre>{{ JSON.stringify(tableDetail.unique_constraints||[],null,2) }}</pre></article><article><h4>外键</h4><pre>{{ JSON.stringify(tableDetail.foreign_keys||[],null,2) }}</pre></article><article><h4>索引</h4><pre>{{ JSON.stringify(tableDetail.indexes||[],null,2) }}</pre></article></div></div></t-drawer>

    <t-drawer :close-btn="true" :close-on-esc-keydown="true" v-model:visible="showRelations" :header="`${selected?.name||''} · 关联关系`" :footer="false" size="min(1040px,96vw)">
      <div class="drawer-body management-controls"><t-alert v-if="relationError" theme="error" :message="relationError" />
      <div class="drawer-toolbar"><t-button v-if="canManage" theme="primary" :disabled="modelingBusy" @click="editSourceRelation()">添加关联关系</t-button><t-button v-if="canManage" :disabled="modelingBusy||!!modelingAction" :loading="modelingAction==='analyze'" @click="analyzeRelations">重新分析关联关系</t-button><t-button v-if="canManage&&canCancelModeling" theme="danger" @click="cancelModeling">取消任务</t-button></div>
      <t-tabs :value="relationStatus" @change="changeRelationStatus"><t-tab-panel value="all" :label="`全部 ${relationCounts.all||0}`" /><t-tab-panel value="accepted" :label="`已生效 ${relationCounts.accepted||0}`" /><t-tab-panel value="pending" :label="`待审核 ${relationCounts.pending||0}`" /><t-tab-panel value="disabled" :label="`已停用 ${relationCounts.disabled||0}`" /></t-tabs>
      <div class="relation-list"><RelationshipCard v-for="r in relationItems" :key="r.id" :source-table="r.source_table_id" :target-table="r.target_table_id" :source-fields="r.source_columns" :target-fields="r.target_columns" :cardinality="r.cardinality" :status="r.status" :reason="r.decision_reason" :busy="modelingBusy||relationBusy" :can-manage="canManage" @edit="editSourceRelation(r)" @decision="action=>action==='disable'?disablingSourceRelation=r:decideSourceRelation(action,r)" /><div v-if="!relationItems.length" class="empty-list">暂无关联关系</div></div>
      <t-pagination v-model="relationPage" :total="relationTotal" :page-size="20" @current-change="loadRelations" /></div>
    </t-drawer>
    <t-dialog v-model:visible="relationEditing" :header="relationForm.id?'编辑关联关系':'添加关联关系'" width="min(800px, 94vw)" :footer="false" :close-on-overlay-click="!relationBusy" :close-btn="!relationBusy" :close-on-esc-keydown="!relationBusy">
      <div class="relation-editor management-controls">
        <t-alert v-if="relationError" theme="error" :message="relationError" />
        <div class="form-grid"><label>来源表<t-select v-model="relationForm.source_table_id" filterable :options="relationTables" @search="searchRelationTables" @change="relationSide('source')" /></label><label>目标表<t-select v-model="relationForm.target_table_id" filterable :options="relationTables" @search="searchRelationTables" @change="relationSide('target')" /></label><label>关联基数<t-select v-model="relationForm.cardinality" :options="knownCardinalities.map(v=>({label:v,value:v}))" placeholder="请选择明确基数" /></label></div>
        <h3>关联条件</h3><div v-for="(pair,i) in relationForm.key_pairs" :key="i" class="pair-row"><t-select v-model="pair.source" filterable :options="relationColumns.source.map(c=>({label:c.name,value:c.name}))" /><span>=</span><t-select v-model="pair.target" filterable :options="relationColumns.target.map(c=>({label:c.name,value:c.name}))" /><t-button variant="text" :disabled="relationForm.key_pairs.length===1" @click="relationForm.key_pairs.splice(i,1)">移除</t-button></div>
        <t-button :disabled="relationForm.key_pairs.length>=8" @click="relationForm.key_pairs.push({source:'',target:''})">添加字段对</t-button>
        <div class="form-actions"><t-button :disabled="relationBusy" @click="relationEditing=false">取消</t-button><t-button :loading="relationBusy" :disabled="modelingBusy||!knownCardinalities.includes(relationForm.cardinality)||!relationForm.source_table_id||!relationForm.target_table_id||relationForm.key_pairs.some((p:any)=>!p.source||!p.target)" theme="primary" @click="saveSourceRelation">保存并生效</t-button></div>
      </div>
    </t-dialog>
    <t-dialog :visible="!!analysisConfirmation" header="确认重新分析关联关系" :confirm-btn="{content:'确认分析',loading:modelingAction==='analyze'}" :cancel-btn="{disabled:!!modelingAction}" :close-btn="!modelingAction" :close-on-overlay-click="!modelingAction" :close-on-esc-keydown="!modelingAction" @confirm="confirmAnalysis" @cancel="analysisConfirmation=null" @close="analysisConfirmation=null"><p>将使用最近一次保存的表结构，重新分析“{{analysisConfirmation?.name}}”的关联关系，可能需要几分钟并消耗模型额度。</p><p>分析期间可取消；原有已生效关系保持可用，完整分析成功后才更新结果。</p><t-alert v-if="pageError" theme="error" :message="pageError" /></t-dialog>
    <t-dialog :visible="!!disablingSourceRelation" header="停用关联关系" :confirm-btn="{content:'确认停用',theme:'danger',loading:relationBusy}" @confirm="decideSourceRelation('disable')" @close="disablingSourceRelation=null">停用后不再进入正式本体，后续重新分析也不会恢复为已生效或待审核；可以在“已停用”中重新启用。</t-dialog>
    <t-dialog v-model:visible="showDrivers" header="管理员 JDBC 驱动" :footer="false" width="720px"><div class="driver-dialog management-controls"><div v-for="driver in drivers" :key="driver.id" class="driver-row"><strong>{{ driver.name }} {{ driver.version }}</strong><span>{{ driver.enabled?'已启用':'未启用' }}</span><t-button v-if="!driver.builtin" size="small" variant="text" @click="editDriver(driver)">编辑</t-button><t-button v-if="!driver.builtin" size="small" variant="text" @click="toggleDriver(driver)">{{ driver.enabled?'停用':'启用' }}</t-button></div><label v-if="!editingDriverId">驱动 JAR<input type="file" accept=".jar" multiple @change="driverFiles=Array.from(($event.target as HTMLInputElement).files||[])" /></label><label>驱动配置 JSON<t-textarea v-model="driverConfig" :autosize="{minRows:8,maxRows:16}" /></label><div class="header-actions"><t-button v-if="editingDriverId" variant="text" @click="editingDriverId=''">取消编辑</t-button><t-button @click="installDriver">{{ editingDriverId?'保存适配':'上传驱动包' }}</t-button></div></div></t-dialog>
  </div>
</template>

<style scoped>
.datasource-page{max-width:1480px}.header-actions,.form-actions{display:flex;flex-wrap:wrap;gap:10px;justify-content:flex-end}.workspace{display:grid;grid-template-columns:300px minmax(0,1fr);min-height:680px;border-top:1px solid var(--semora-line);overflow:hidden}.source-sidebar{background:#f7f9fc;border-right:1px solid var(--semora-line);padding:14px 12px}.source-search{padding:2px 2px 14px}.source-item{width:100%;display:grid;grid-template-columns:38px minmax(0,1fr) 10px;align-items:center;gap:11px;padding:13px 12px;border:0;border-radius:6px;background:transparent;text-align:left;cursor:pointer;margin-bottom:6px}.source-item:hover{background:#fff}.source-item.active{background:#e5effb;color:var(--management-blue)}.source-copy strong,.source-copy small{display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.source-copy small{font-size:12px;color:var(--semora-muted);margin-top:3px}.connection-dot{width:8px;height:8px;border-radius:50%;background:#aeb9c7}.connection-dot.healthy{background:#0eb86b}.connection-dot.unsafe{background:#e34d59}.source-form-panel{padding:30px 34px}.form-heading{display:flex;justify-content:space-between;align-items:flex-start;padding-bottom:22px;border-bottom:1px solid var(--semora-line)}.form-heading h2{margin:0;font-size:22px;color:var(--semora-ink)}.modeling-status{margin:22px 0;padding:16px 18px;border-radius:11px;background:#f3f7fc}.modeling-status>div{display:flex;justify-content:space-between;margin-bottom:9px}.modeling-status .t-button{margin-top:12px}.modeling-status small{display:block;margin-top:8px;color:var(--semora-muted)}.error-text{color:#c9353f!important}.form-section{padding:25px 0}.section-title h3{margin:0;color:#173b68}.section-title p{margin:6px 0 20px;color:var(--semora-muted)}.form-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:18px 20px}.form-grid label,.driver-dialog label{display:flex;flex-direction:column;gap:8px;font-weight:600;color:#243c5a}.span-2{grid-column:1/-1}.advanced{margin-top:20px}.advanced summary{cursor:pointer;color:#526b88}.advanced-grid{margin-top:18px}.connection-result{display:flex;align-items:center;gap:10px;padding:13px 16px;border-radius:9px;background:#f5f7fa}.connection-result.success{color:#087b49;background:#ecfbf4}.connection-result.error{color:#b4232d;background:#fff0f1}.connection-result.testing{color:#315b8d;background:#f0f5fb}.result-dot{width:9px;height:9px;border-radius:50%;background:currentColor}.blank-state,.empty-list{text-align:center;color:var(--semora-muted);padding:55px 20px}.blank-state h3{color:#173b68}.drawer-body,.schema-detail,.relation-editor{padding:4px 8px 20px}.drawer-toolbar{display:flex;gap:10px;margin-bottom:18px}.drawer-toolbar .t-input,.drawer-toolbar .t-select{max-width:420px}.data-table{border:1px solid var(--semora-line);border-radius:10px;overflow:hidden;margin-bottom:18px}.table-row{display:grid;grid-template-columns:2fr .7fr 1.5fr .5fr 110px;gap:12px;align-items:center;min-height:58px;padding:9px 14px;border-top:1px solid var(--semora-line)}.table-row:first-child{border-top:0}.table-head{min-height:42px;background:#f5f7fa;font-weight:600;color:#526b88}.table-row small{display:block;color:var(--semora-muted);margin-top:3px}.schema-title{padding:16px 0}.schema-title h3{margin:0}.schema-title p{color:var(--semora-muted)}.column-table{border:1px solid var(--semora-line);border-radius:10px;overflow:hidden}.column-head,.column-line{display:grid;grid-template-columns:1.2fr 1fr .5fr 1.8fr;gap:12px;padding:11px 14px;border-top:1px solid var(--semora-line)}.column-head{border-top:0;background:#f5f7fa;font-weight:600}.constraint-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-top:18px}.constraint-grid article{border:1px solid var(--semora-line);border-radius:10px;padding:14px}.constraint-grid h4{margin:0 0 8px}.constraint-grid pre{max-height:180px;overflow:auto;white-space:pre-wrap;color:#526b88}.relation-list{display:flex;flex-direction:column;gap:10px;margin:14px 0 18px}.relation-reason{margin:12px 0 0;color:var(--semora-muted);font-size:13px;line-height:1.6;overflow-wrap:anywhere}.relation-expression{display:grid;grid-template-columns:1fr 60px 1fr;gap:14px;align-items:center}.relation-expression>span{min-width:0}.relation-expression strong,.relation-expression code{display:block;overflow-wrap:anywhere}.relation-expression code{margin-top:6px;color:#526b88}.relation-expression>b{text-align:center;color:#173b68}.relation-meta{display:flex;gap:8px;align-items:center;margin-top:14px;padding-top:12px;border-top:1px solid var(--semora-line);color:var(--semora-muted)}.relation-actions{margin-left:auto}.pair-editor{margin-top:24px}.pair-head{display:flex;justify-content:space-between;align-items:center}.pair-row{display:grid;grid-template-columns:1fr 28px 1fr 70px;gap:10px;align-items:center;margin-bottom:10px}.relation-editor .form-actions{margin-top:24px}.driver-dialog{display:flex;flex-direction:column;gap:16px}.driver-row{display:flex;justify-content:space-between;padding:10px;border-bottom:1px solid var(--semora-line)}
@media(max-width:760px){.source-sidebar{border-right:0;border-bottom:1px solid var(--semora-line);overflow:auto}.form-grid,.constraint-grid{grid-template-columns:1fr}.span-2{grid-column:auto}.source-form-panel{padding:22px}.table-row{grid-template-columns:1.5fr .7fr .5fr 90px}.table-row>span:nth-child(3){display:none}}
.datasource-page{max-width:none;width:100%;display:flex;flex-direction:column}.datasource-page>.page-header{flex-shrink:0}.workspace{flex:1;min-height:0;grid-template-columns:280px minmax(0,1fr)}.source-sidebar{overflow:auto}.source-form-panel{min-width:0;padding:24px;overflow:auto}.form-heading{padding-bottom:18px}.source-subtitle{margin:6px 0 0;font-size:13px;color:var(--semora-muted)}.source-overview{display:grid;grid-template-columns:minmax(0,1.45fr) minmax(250px,1fr);gap:28px;align-items:start}.detail-panel{border-top:1px solid var(--semora-line);padding:18px 0 0;min-width:0}.detail-panel-heading{display:flex;justify-content:space-between;gap:12px;align-items:center}.detail-panel-heading h3{font-size:16px;margin:0;color:var(--semora-ink)}.panel-caption{font-size:12px;line-height:1.7;color:var(--semora-muted);margin:12px 0 18px}.source-name-row{display:flex;gap:12px;align-items:end;margin-top:20px}.source-name-row label{flex:1;min-width:0;display:flex;flex-direction:column;gap:8px;font-size:13px}.connection-facts{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:22px 20px;margin:24px 0}.connection-facts div{min-width:0}.connection-facts dt{font-size:12px;color:var(--semora-muted);margin-bottom:7px}.connection-facts dd{font-size:14px;margin:0;overflow-wrap:anywhere;line-height:1.6}.task-panel .modeling-status{margin:20px 0;padding:16px;background:var(--semora-page)}.task-facts{display:grid;gap:12px;margin:20px 0}.task-facts>div{display:flex;justify-content:space-between;align-items:baseline;gap:16px;font-size:12px}.task-facts span{color:var(--semora-muted)}.task-facts strong{font-weight:500;text-align:right;overflow-wrap:anywhere}.task-actions{display:flex;gap:10px;flex-wrap:wrap;padding-top:18px;border-top:1px solid var(--semora-line)}.task-hint{margin:16px 0 0}.connection-panel .connection-result{margin-top:20px;font-size:12px}.advanced{font-size:13px}.advanced .connection-facts{margin-bottom:0}@media(min-width:1700px){.connection-facts{grid-template-columns:repeat(3,minmax(0,1fr))}}@media(max-width:600px){.connection-facts{grid-template-columns:1fr}.source-name-row{align-items:stretch;flex-direction:column}}
.source-title { display: flex; align-items: center; gap: 12px; min-width: 0; overflow-wrap: anywhere; }
.form-heading { gap: 14px; }
.source-list-empty { display: flex; flex-direction: column; align-items: center; gap: 6px; padding: 22px 6px; color: var(--semora-muted); font-size: 13px; }
/* Keep the navigator aligned with the detail header and use one vertical divider. */
.workspace { border: 0; grid-template-columns: 248px minmax(0, 1fr); }
.source-sidebar { padding: 0 16px 16px 0; background: transparent; }
.source-search { padding: 0 0 14px; }
.source-item { grid-template-columns: 36px minmax(0, 1fr) 8px; gap: 12px; min-height: 64px; padding: 8px 10px; margin-bottom: 3px; }
.source-item:hover { background: #f3f7fb; }
.source-item.active { background: #e7f0fc; }
.source-copy strong { font-size: 14px; }
.source-form-panel { padding: 0 0 20px 30px; }
.form-heading { align-items: center; padding: 0; border: 0; flex-wrap: wrap; }
.source-title-line { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.source-title .database-icon { --database-icon-size: 44px; }
.resource-links { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; }
.form-heading { margin-bottom: 22px; }
.rename-source { margin-top: 14px; }
.rename-source .source-name-row { margin-top: 10px; }
.resource-links .t-icon { color: var(--management-blue); }
.source-overview { gap: 28px; }
.detail-panel { padding-top: 18px; }
.detail-panel-heading h3 { font-size: 17px; }
.panel-caption, .connection-facts dt, .task-facts > div { font-size: 13px; }
.connection-panel > .connection-facts { margin: 16px 0; gap: 0 20px; }
.connection-panel > .connection-facts > div { padding: 9px 0; border-bottom: 1px solid var(--semora-line); }
.connection-panel > .connection-facts dt { margin-bottom: 3px; }
.source-name-row :deep(.t-button) { min-height: 36px; }
.relation-list { gap: 0; }
.task-state { display: grid; gap: 6px; margin-top: 13px; color: var(--semora-muted); font-size: 13px; }
.task-stop, .task-phase { display: flex; flex-wrap: wrap; align-items: baseline; gap: 4px 10px; }
.task-phase { justify-content: space-between; color: var(--semora-ink); }
.task-state p { margin: 0; overflow-wrap: anywhere; }
.task-facts { gap: 8px; margin: 14px 0; }
.task-facts dt { color: var(--semora-muted); }
.task-facts dd { margin: 0; text-align: right; font-variant-numeric: tabular-nums; }
.task-actions { padding-top: 12px; }
.scan-outcome { margin: 10px 0 0; color: #137047; font-size: 13px; overflow-wrap: anywhere; }
@media (max-width: 1060px) { .source-overview { grid-template-columns: 1fr; } }
@media (max-width: 760px) {
  .workspace { grid-template-columns: 1fr; min-height: 0; overflow: visible; }
  .source-sidebar { padding-right: 0; max-height: 215px; }
  .source-form-panel { padding: 20px 0 0; overflow: visible; }
  .datasource-page { display: block; }
}
</style>
