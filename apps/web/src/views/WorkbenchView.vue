<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { api, type QueryRun, type QuerySession } from '../api'
import { AddIcon, DeleteIcon } from 'tdesign-icons-vue-next'
import QueryTaskDetail from '../components/query/QueryTaskDetail.vue'
import { useRunConnection } from '../composables/useRunConnection'

const sessions = ref<QuerySession[]>([])
const domains = ref<any[]>([])
const queryableDomains = computed(() => domains.value.filter(domain => domain.status !== 'unconfigured'))
const activeSession = ref('')
const selectedDomain = ref('')
const requirementForm = reactive({ title: '', objective: '', time_range: '', scope: '', output: '' })
const modelConfigured = ref(false)
const initializing = ref(true)
const pageError = ref('')
const sessionStatus = ref('all')
const sessionSearch = ref('')
const sessionPage = ref(1)
const sessionPageSize = 10
const deletingSession = ref<QuerySession | null>(null)
const deleting = ref(false)
const deletionError = ref('')
let sessionListVersion = 0
const run = ref<QueryRun | null>(null)
const loading = ref(false)
let selectionVersion = 0
const alignmentAnswers = ref<Record<string, string>>({})
const { state: connectionState, lastSignal, start: connectRun, stop: stopStream } = useRunConnection(run)
const activeSessionInfo = computed(() => sessions.value.find(item => item.id === activeSession.value))
function statusGroup(status: string | null | undefined) {
  if (!status) return 'not_started'
  if (['clarifying', 'awaiting_approval'].includes(status)) return 'clarifying'
  return ['completed', 'failed'].includes(status) ? status : 'processing'
}
function statusLabel(session: QuerySession) {
  return ({ not_started: '未开始', clarifying: '待确认', completed: '已完成', failed: '未完成', processing: '进行中' } as Record<string, string>)[statusGroup(session.latest_status)]
}
const filteredSessions = computed(() => sessions.value
  .filter(item => item.domain === selectedDomain.value
    && (sessionStatus.value === 'all' || statusGroup(item.latest_status) === sessionStatus.value)
    && item.title.toLocaleLowerCase().includes(sessionSearch.value.trim().toLocaleLowerCase()))
  .slice().sort((a, b) => Date.parse(b.created_at) - Date.parse(a.created_at)))
const sessionPages = computed(() => Math.max(1, Math.ceil(filteredSessions.value.length / sessionPageSize)))
const visibleSessions = computed(() => filteredSessions.value.slice(
  (sessionPage.value - 1) * sessionPageSize, sessionPage.value * sessionPageSize,
))
watch([sessionSearch, sessionStatus, selectedDomain], () => { sessionPage.value = 1 })
watch(sessionPages, pages => { sessionPage.value = Math.min(sessionPage.value, pages) })
watch(() => run.value?.status, status => {
  const session = sessions.value.find(item => item.id === run.value?.session_id)
  if (session && status) session.latest_status = status
})
const activeDomain = computed(() => run.value?.domain || activeSessionInfo.value?.domain || selectedDomain.value)
const activeDomainName = computed(() => domains.value.find(item => item.domain === activeDomain.value)?.name || activeDomain.value)
function resetAlignmentAnswers() {
  alignmentAnswers.value = {}
}
async function loadSessions() {
  const version = ++sessionListVersion
  const { data } = await api.get<QuerySession[]>('/sessions')
  if (version === sessionListVersion) sessions.value = data
}
function openDeleteSession(session: QuerySession) {
  deletionError.value = ''
  deletingSession.value = session
}
async function deleteSession() {
  const session = deletingSession.value
  if (!session || deleting.value) return
  deleting.value = true
  deletionError.value = ''
  try {
    await api.delete(`/sessions/${encodeURIComponent(session.id)}`)
    sessionListVersion += 1
    sessions.value = sessions.value.filter(item => item.id !== session.id)
    deletingSession.value = null
    if (activeSession.value === session.id) createSession()
  } catch (error: any) {
    deletionError.value = error.response?.data?.detail || '删除查询失败，请稍后重试。'
  } finally {
    deleting.value = false
  }
}
async function switchDomain(value: unknown) {
  if (typeof value !== 'string' || !queryableDomains.value.some(domain => domain.domain === value)) return
  selectedDomain.value = value
  sessionStatus.value = 'all'
  sessionSearch.value = ''
  createSession()
  const latest = sessions.value.find(item => item.domain === value)
  if (latest) await selectSession(latest)
}
function sessionDate(value: string) {
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? '' : date.toLocaleString('zh-CN', { year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false })
}
function createSession() {
  selectionVersion += 1
  stopStream()
  resetAlignmentAnswers()
  loading.value = false
  activeSession.value = ''
  run.value = null
  pageError.value = ''
  Object.assign(requirementForm, { title: '', objective: '', time_range: '', scope: '', output: '' })
}
async function submitRequirement() {
  if (!requirementForm.title.trim() || !requirementForm.objective.trim() || !modelConfigured.value || loading.value) return
  if (!selectedDomain.value) { pageError.value = '请先选择业务域'; return }
  const version = selectionVersion
  const form = { ...requirementForm }
  const domain = selectedDomain.value
  pageError.value = ''
  loading.value = true
  try {
    const body = { title: form.title.trim(), domain, requirement_form: form }
    const current = sessions.value.find(item => item.id === activeSession.value)
    let sessionId = current?.id || ''
    if (current && !current.run_count && current.domain === domain) {
      await api.put(`/sessions/${current.id}/requirement`, body)
    } else {
      sessionId = (await api.post('/sessions', body)).data.id
    }
    if (version !== selectionVersion) return
    activeSession.value = sessionId
    await loadSessions()
    if (version !== selectionVersion) return
    loading.value = false
    await submitRequest(form.objective)
  } catch (error: any) {
    if (version === selectionVersion) pageError.value = error?.response?.data?.detail || '需求提交失败'
  } finally { if (version === selectionVersion) loading.value = false }
}
async function selectSession(session: QuerySession) {
  if (session.domain !== selectedDomain.value) return
  createSession()
  const version = selectionVersion
  activeSession.value = session.id
  Object.assign(requirementForm, session.requirement_form || {})
  try {
    const { data } = await api.get<QueryRun[]>(`/sessions/${session.id}/runs`, { params: { limit: 1 } })
    if (version !== selectionVersion) return
    run.value = data[0] || null
    if (run.value && !['clarifying', 'completed', 'failed', 'awaiting_approval'].includes(run.value.status)) {
      loading.value = true
      await connectRun(run.value.id)
    }
  } catch { if (version === selectionVersion) pageError.value = '暂时无法加载查询，请重新打开这条记录。' }
  finally { if (version === selectionVersion) loading.value = false }
}
async function submitRequest(text: string, mode: 'new' | 'continue' = 'new') {
  const value = text.trim()
  if (!value || loading.value) return
  const version = selectionVersion
  const sessionId = activeSession.value
  pageError.value = ''
  loading.value = true
  try {
    const { data } = await api.post<QueryRun>(`/sessions/${sessionId}/messages`, { question: value, mode })
    if (version !== selectionVersion) return
    run.value = data
    void loadSessions().catch(() => {})
    resetAlignmentAnswers()
    if (!['clarifying', 'completed', 'failed'].includes(data.status)) await connectRun(data.id)
    if (version === selectionVersion) await loadSessions()
  } catch (error: any) {
    if (version === selectionVersion) pageError.value = error?.response?.data?.detail || '分析请求失败'
  } finally { if (version === selectionVersion) loading.value = false }
}
async function retryCurrent() {
  if (run.value) await submitRequest('继续', 'continue')
}
async function confirmAlignment() {
  if (!run.value || loading.value) return
  const version = selectionVersion
  const current = run.value
  loading.value = true
  try {
    const { data } = await api.post<QueryRun>(`/runs/${current.id}/alignment`, { answers: { ...alignmentAnswers.value }, draft_version: current.requirement_draft?.version })
    if (version !== selectionVersion) return
    run.value = data
    resetAlignmentAnswers()
    if (!['clarifying', 'completed', 'failed', 'awaiting_approval'].includes(data.status)) await connectRun(data.id)
    if (version === selectionVersion) await loadSessions()
  } catch (error: any) {
    if (version !== selectionVersion) return
    pageError.value = error?.response?.data?.detail || '补充分析连接中断，请查看任务最新状态'
    try {
      const { data } = await api.get<QueryRun>(`/runs/${current.id}`)
      if (version === selectionVersion) run.value = data
    } catch { /* Keep the last known run. */ }
  } finally { if (version === selectionVersion) loading.value = false }
}
async function cancelRun() {
  if (!run.value) return
  const version = selectionVersion
  const id = run.value.id
  await api.post(`/executions/${id}/cancel`)
  const { data } = await api.get<QueryRun>(`/runs/${id}`)
  if (version !== selectionVersion) return
  run.value = data
  loading.value = false
}
onMounted(async () => {
  const version = selectionVersion
  try {
    await Promise.all([
      loadSessions(),
      api.get('/domains').then(({ data }) => { domains.value = data }),
      api.get('/model/status').then(({ data }) => { modelConfigured.value = data.configured }),
    ])
    if (version !== selectionVersion) return
    if (!queryableDomains.value.some(item => item.domain === selectedDomain.value)) {
      selectedDomain.value = queryableDomains.value[0]?.domain || ''
    }
    initializing.value = false
    const current = sessions.value.find(item => item.domain === selectedDomain.value)
    if (current) await selectSession(current)
  } catch {
    if (version === selectionVersion) pageError.value = '工作台暂未能加载，请刷新页面重试。'
  } finally { initializing.value = false }
})
</script>

<template>
  <div class="workbench">
    <aside class="session-list">
      <div class="session-head"><strong>查询工作台</strong></div>
      <div class="domain-context">
        <span class="domain-caption">当前业务域</span>
        <t-select v-model="selectedDomain" class="domain-picker" :placeholder="!initializing && !queryableDomains.length ? '暂无已绑定数据源的业务域' : '选择业务域'" @change="switchDomain">
          <t-option v-for="domain in queryableDomains" :key="domain.domain" :value="domain.domain" :label="domain.name" />
        </t-select>
      </div>
      <button class="new-query" :disabled="!selectedDomain" @click="createSession"><AddIcon /><span>新建查询</span></button>
      <div class="session-group"><span>查询记录 · {{ filteredSessions.length }}</span><select v-model="sessionStatus" aria-label="筛选任务状态"><option value="all">全部状态</option><option value="processing">进行中</option><option value="clarifying">待确认</option><option value="completed">已完成</option><option value="failed">未完成</option></select></div>
      <div class="session-search"><t-input v-model="sessionSearch" clearable aria-label="按查询名称搜索" placeholder="搜索查询名称" /></div>
      <div class="session-records">
        <div v-for="session in visibleSessions" :key="session.id" class="session-row">
          <button class="session-item" :class="{ active: activeSession === session.id }" :title="session.title" @click="selectSession(session)">
            <span>{{ session.title }}</span><small><time :datetime="session.created_at">{{ sessionDate(session.created_at) }}</time><span class="session-status" :class="statusGroup(session.latest_status)">{{ statusLabel(session) }}</span></small>
          </button>
          <button type="button" class="session-delete" :aria-label="`删除查询 ${session.title}`"
                  :title="statusGroup(session.latest_status) === 'processing' ? '请先等待查询完成或取消查询' : '删除查询'"
                  :disabled="statusGroup(session.latest_status) === 'processing'" @click="openDeleteSession(session)"><DeleteIcon /></button>
        </div>
        <div v-if="!filteredSessions.length && !initializing" class="session-empty">
          <strong>{{ sessionSearch.trim() ? '没有匹配的查询' : sessionStatus !== 'all' ? '暂无该状态的查询' : '还没有查询记录' }}</strong>
          <p>{{ sessionSearch.trim() ? '试试其他名称关键词。' : sessionStatus !== 'all' ? '可切换状态查看其他查询。' : '点击“新建查询”，开始这个业务域的第一次查询。' }}</p>
        </div>
      </div>
      <div v-if="sessionPages > 1" class="session-pagination" aria-label="查询记录分页">
        <t-button size="small" variant="text" :disabled="sessionPage === 1" @click="sessionPage--">上一页</t-button>
        <span aria-live="polite">{{ sessionPage }} / {{ sessionPages }}</span>
        <t-button size="small" variant="text" :disabled="sessionPage === sessionPages" @click="sessionPage++">下一页</t-button>
      </div>
      <div class="session-tip"><span class="context-indicator"></span><span>仅显示当前业务域的查询</span></div>
    </aside>

    <t-dialog :visible="!!deletingSession" header="删除查询"
              :confirm-btn="{ content: '确认删除', theme: 'danger', loading: deleting, disabled: deleting }"
              :cancel-btn="{ disabled: deleting }" :close-btn="!deleting" :close-on-overlay-click="false"
              :close-on-esc-keydown="!deleting" @confirm="deleteSession"
              @cancel="deletingSession = null" @close="deletingSession = null">
      <p>确定删除「{{ deletingSession?.title }}」？该查询及其分析记录将从工作台移除。</p>
      <t-alert v-if="deletionError" theme="error" :message="deletionError" />
    </t-dialog>

    <section class="query-space">
      <QueryTaskDetail v-if="run" :run="run" :title="activeSessionInfo?.title || '本次查询'" :domain-name="activeDomainName" :original-request="activeSessionInfo?.requirement_form?.objective" :loading="loading" :connection="connectionState" :last-signal="lastSignal" v-model:answers="alignmentAnswers" @confirm="confirmAlignment" @retry="retryCurrent" @cancel="cancelRun"/>
      <div v-else-if="initializing" class="workbench-loading" role="status">正在加载工作台…</div>
      <div v-else class="welcome-scroll">
        <header class="welcome-header"><span>{{ activeDomainName || '查询工作台' }}</span><strong>新建查询</strong></header>
        <div class="query-composer">
          <div class="composer-eyebrow">SEMORA · 业务查询</div><h1>您想了解什么？</h1><p class="composer-intro">描述您的业务问题，必要时我们会向您确认关键口径。</p>
          <t-alert v-if="!modelConfigured" theme="warning" title="请先配置模型"><template #message>配置完成后即可开始查询。<router-link to="/settings#model">前往配置</router-link></template></t-alert>
          <form class="requirement-form" @submit.prevent="submitRequirement">
            <label class="title-label">查询名称 <span class="required-mark">*</span><input v-model="requirementForm.title" required maxlength="100" placeholder="为本次查询取一个便于查找的名称" /></label>
            <label class="objective-label">查询需求 <span class="required-mark">*</span><textarea v-model="requirementForm.objective" required maxlength="1500" rows="7" placeholder="请说明您想了解什么。建议写清时间范围、涉及的数据范围，以及希望查看的明细或汇总结果；不确定的地方可以直接说明，我们会协助您确认。" /></label>
            <div class="composer-footer"><span>本次查询将归属 {{ activeDomainName || '所选业务域' }}</span><t-button type="submit" theme="primary" :loading="loading" :disabled="!modelConfigured || !selectedDomain || !requirementForm.title.trim() || !requirementForm.objective.trim()">提交需求，开始分析</t-button></div>
          </form>
        </div>
      </div>
      <div v-if="pageError" class="page-error" role="alert">{{ pageError }}</div>
    </section>
  </div>
</template>

<style scoped>
.workbench{display:flex;height:100%;background:var(--semora-card)}
.session-list{width:280px;flex:none;display:flex;flex-direction:column;background:var(--semora-sidebar);border-right:1px solid var(--semora-line);padding:26px 16px 16px;overflow:hidden}
.session-head{padding:0 8px 22px;font-size:19px;letter-spacing:-.4px}
.domain-context{padding:0 4px}
.domain-caption{display:block;font-size:11px;color:var(--semora-muted);margin:0 0 9px 4px;letter-spacing:.6px}
.domain-picker :deep(.t-input){height:40px;border-radius:9px;background:var(--semora-card)}
.new-query{width:100%;height:40px;display:flex;align-items:center;justify-content:center;gap:8px;border:0;border-radius:9px;margin:14px 0 18px;background:var(--semora-green);color:white;font-weight:600;cursor:pointer;transition:background .15s}
.new-query:hover{background:var(--semora-green-dark)}
.new-query:disabled{opacity:.45;cursor:not-allowed}
.session-group{display:flex;align-items:center;justify-content:space-between;margin:22px 10px 10px;font-size:11px;color:var(--semora-muted)}
.session-records{flex:1;min-height:0;overflow-y:auto;display:flex;flex-direction:column;gap:3px;padding:0 2px 12px;scrollbar-width:thin}
.session-search{margin:0 2px 9px}
.session-row{display:flex;align-items:center;gap:2px;flex-shrink:0}
.session-row .session-item{flex:1;min-width:0}
.session-delete{display:grid;place-items:center;width:28px;height:28px;flex:none;border:0;border-radius:6px;background:transparent;color:var(--semora-muted);cursor:pointer}
.session-delete:hover:not(:disabled){color:var(--td-error-color);background:var(--semora-green-soft)}
.session-delete:disabled{opacity:.35;cursor:not-allowed}
.session-pagination{display:flex;align-items:center;justify-content:space-between;color:var(--semora-muted);font-size:12px;padding:6px 2px}
.session-item{border:0;background:transparent;text-align:left;color:var(--semora-ink);cursor:pointer;width:100%;flex-shrink:0;padding:12px 11px;border-radius:8px;transition:background .15s}
.session-item:hover{background:var(--semora-green-soft)}
.session-item span{display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:13px;line-height:1.5;font-weight:500}
.session-item small{display:block;font-size:11px;margin-top:6px;opacity:.85;color:var(--semora-muted)}
.session-item.active{background:var(--semora-card);box-shadow:0 1px 4px rgba(0,0,0,.04);color:var(--semora-green-dark)}
.session-empty{padding:30px 10px;text-align:center;color:var(--semora-muted)}
.session-empty strong{font-size:12px;font-weight:500}
.session-empty p{font-size:11px;line-height:1.8;margin:8px 0}
.session-tip{flex-shrink:0;display:flex;align-items:center;gap:7px;padding:16px 8px 0;font-size:11px;color:var(--semora-muted);border-top:1px solid var(--semora-line)}
.context-indicator{width:5px;height:5px;border-radius:50%;background:var(--semora-green)}
.query-space{flex:1;min-width:0;height:100%;display:flex;flex-direction:column}
.task-detail{flex:1;min-height:0}
.welcome-scroll{flex:1;min-height:0;overflow:auto}
.welcome-header{height:84px;border-bottom:1px solid var(--semora-line);padding:22px 34px;display:flex;flex-direction:column;gap:5px}
.welcome-header span{font-size:11px;color:var(--semora-muted)}
.welcome-header strong{font-size:15px;font-weight:500}
.query-composer{max-width:800px;padding:0 34px;margin:60px auto 44px}
.composer-eyebrow{font-size:11px;font-weight:500;letter-spacing:1.3px;color:var(--semora-green-dark)}
.query-composer h1{font-size:29px;font-weight:600;letter-spacing:-.6px;margin:12px 0 9px}
.composer-intro{font-size:13px;line-height:1.8;color:var(--semora-muted);margin:0 0 32px}
.requirement-form{display:flex;flex-direction:column;gap:21px}
.requirement-form label{font-size:12px;display:block;color:var(--semora-ink)}
.objective-label textarea{display:block;width:100%;margin-top:9px;border:1px solid var(--semora-line);border-radius:11px;padding:17px 18px;font-size:15px;line-height:1.9;min-height:170px;resize:vertical;color:var(--semora-ink);background:var(--semora-card);outline:0}
.objective-label textarea:focus{border-color:var(--semora-green);box-shadow:0 0 0 3px var(--semora-green-soft)}
.objective-label textarea::placeholder{color:var(--semora-muted);font-size:14px}
.composer-footer{display:flex;align-items:center;justify-content:space-between;gap:14px}
.composer-footer>span{font-size:11px;color:var(--semora-muted)}
.workbench-loading{padding:60px 34px;color:var(--semora-muted);font-size:13px}
.page-error{padding:12px 26px;background:color-mix(in srgb,#e34d59 9%,var(--semora-card));color:#c24951;font-size:13px;border-top:1px solid var(--semora-line)}
@media(max-width:1100px){.session-list{width:250px}
.query-composer{margin-top:40px}
.composer-footer{align-items:flex-start;flex-direction:column}
}

.session-list{width:268px;padding:28px 14px 18px;background:var(--semora-sidebar)}

.session-head{font-size:18px;font-weight:600;padding-bottom:25px;letter-spacing:.2px}

.new-query{height:42px;margin-bottom:4px;border-radius:6px}

.session-group{margin:20px 8px 12px;gap:8px}

.session-group select{max-width:106px;border:1px solid var(--semora-line);border-radius:5px;background:var(--semora-card);color:var(--semora-muted);padding:5px 7px;font-size:11px;cursor:pointer}

.session-records{gap:5px;overscroll-behavior:contain}

.session-item{padding:14px 11px;border:1px solid transparent;border-radius:6px}

.session-item.active{border-color:var(--semora-line);box-shadow:0 2px 6px #10233f06}

.session-item small{display:flex;justify-content:space-between;align-items:center;gap:5px;font-size:10px;margin-top:10px}

.session-item small .session-status{display:inline;overflow:visible;font-size:10px;font-weight:400;color:var(--semora-muted);flex-shrink:0}

.session-status::before{content:'';display:inline-block;width:5px;height:5px;border-radius:50%;background:currentColor;margin-right:4px;vertical-align:middle}

.session-item small .processing{color:#416ea8}
.session-item small .clarifying{color:#a77b30}
.session-item small .completed{color:#35806b}
.session-item small .failed{color:#ba616b}

.query-space{background:var(--semora-page)}

.welcome-header{background:var(--semora-card);height:88px;padding:23px 32px}

.query-composer{max-width:820px;margin:44px auto;padding:36px 40px 40px;background:var(--semora-card);border:1px solid var(--semora-line);border-radius:10px;box-shadow:0 8px 32px #10233f03}

.query-composer h1{font-size:26px}
.composer-intro{margin-bottom:30px}

.title-label input{display:block;width:100%;margin-top:9px;padding:12px 14px;border:1px solid var(--semora-line);border-radius:6px;background:var(--semora-card);color:var(--semora-ink);outline:0}

.title-label input:focus{border-color:var(--semora-green);box-shadow:0 0 0 3px var(--semora-green-soft)}

.required-mark{color:#b56767;font-size:12px;margin-left:3px}

.objective-label textarea{font-size:14px;border-radius:6px;padding:14px;min-height:210px}
.objective-label textarea::placeholder{font-size:13px;line-height:1.9}

.composer-footer{padding-top:6px}
.composer-footer .t-button{border-radius:6px}

@media(max-width:1100px){.session-list{width:250px}
.query-composer{margin:24px;padding:28px}
}

</style>
