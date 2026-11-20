<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { HistoryIcon, ChevronRightIcon } from 'tdesign-icons-vue-next'
import type { QueryRun } from '../../api'
import { visibleQuestions, freeAnswerText } from '../../queryQuestions'
import { businessFailure, resultDescription, terminalStatus, sqlFailure } from '../../queryPresentation'
import { displayValue } from '../../analysisTimeline'
import TaskStatus from './TaskStatus.vue'
import ExecutionRecord from './ExecutionRecord.vue'
import { useOntologyNames } from '../../composables/useOntologyNames'

const props = defineProps<{
  run: QueryRun; title: string; domainName: string; loading: boolean; originalRequest?: string
  connection: string; lastSignal: number; answers: Record<string, string>
}>()
const emit = defineEmits<{
  'update:answers': [value: Record<string, string>]; confirm: []; retry: []; cancel: []
}>()
const recordOpen = ref(false)
const readable=useOntologyNames(computed(()=>props.run.domain))
const resultPage = ref(1)
const resultPageSize = ref(10)
const resultTable = ref<HTMLDivElement | null>(null)
const previewRows = computed(() => (props.run.result?.rows || []).slice(0, 1000))
const resultPageCount = computed(() => Math.max(1, Math.ceil(previewRows.value.length / resultPageSize.value)))
const pageRows = computed(() => previewRows.value.slice((resultPage.value - 1) * resultPageSize.value, resultPage.value * resultPageSize.value))
const previewStart = computed(() => previewRows.value.length ? (resultPage.value - 1) * resultPageSize.value + 1 : 0)
const previewEnd = computed(() => Math.min(resultPage.value * resultPageSize.value, previewRows.value.length))
watch(() => props.run.id, () => { recordOpen.value = false; resultPage.value = 1; resultPageSize.value = 10 })
watch(() => previewRows.value.length, () => { resultPage.value = Math.min(resultPage.value, resultPageCount.value) })
watch([resultPage, resultPageSize, () => props.run.id], () => { if (resultTable.value) resultTable.value.scrollTop = 0 })
function changeResultPage(current: number) { resultPage.value = Math.max(1, Math.min(current, resultPageCount.value)) }
function changeResultPageSize(event: Event) {
  const size = Number((event.target as HTMLSelectElement).value)
  if (![10, 20, 50].includes(size)) return
  resultPageSize.value = size
  resultPage.value = 1
}
const questions = computed(() => visibleQuestions(props.run.requirement_draft?.clarifications || [], props.answers))
const gaps = computed(() => {
  const ids = new Set(questions.value.map(q => q.id))
  return (props.run.query_rehearsal?.technical_gaps || props.run.requirement_draft?.technical_gaps || [])
    .filter(g => (g.depends_on || []).every(c => ids.has(c.question_id) && c.values.includes(props.answers[c.question_id])))
    .map(g=>({...g,summary:readable(g.summary),resolution:readable(g.resolution),objects:g.objects.map(readable),field_refs:g.field_refs.map(readable)}))
})
const ready = computed(() => !gaps.value.length && questions.value.filter(q => q.blocking).every(q => !!props.answers[q.id]?.trim()))
const configurationOnly = computed(() => !questions.value.length && !!gaps.value.length)
const basis = computed(() => readable(props.run.requirement?.summary || props.run.requirement_draft?.summary || props.run.query_rehearsal?.summary || ''))
const statisticsRules = computed(() => {
  const draft = props.run.requirement_draft
  const confirmed = props.run.requirement
  const currentDraft = !confirmed || (draft && (confirmed.source_draft_version == null || confirmed.source_draft_version === draft.version))
  const candidates = currentDraft
    ? [draft?.analysis_steps, props.run.query_rehearsal?.skeleton, confirmed?.analysis_steps]
    : [confirmed?.analysis_steps]
  const rules: unknown[] = candidates.find(items => Array.isArray(items) && items.some(item => typeof item === 'string' && item.trim())) || []
  return [...new Set(rules.filter((item): item is string => typeof item === 'string').map(item => readable(item.trim())).filter(Boolean))]
})
const labels = computed(() => props.run.plan?.sql_program?.steps?.at(-1)?.output_labels || {})
const explanation = computed(() => resultDescription(props.run))
const active = computed(() => !terminalStatus(props.run.status))
const failedSQL = computed(() => sqlFailure(props.run))
const currentStep = computed(() => {
  if (props.run.status === 'executing') {
    const started = [...props.run.events].reverse().find(e => ['source.step.started', 'source.step.retry'].includes(e.event))
    const step = props.run.plan?.sql_program?.steps?.find((s: any) => s.name === started?.data.step)
    const completed = [...props.run.events].reverse().find(e => ['source.step.completed', 'source.step.skipped'].includes(e.event) && e.data.step === started?.data.step)
    if (step?.purpose && !completed) return started?.event === 'source.step.retry' ? `本条查询超时，正在重试（第2次，共2次）：${step.purpose}` : step.purpose
  }
  return ({ received: '正在准备本次查询', retrieving: '正在查阅与需求相关的业务资料', understanding: '正在理解需求，核对所需业务信息', aligning: '正在整理需要确认的口径', aligned: '正在将已确认口径落实到查询', planning: '正在准备查询', compiling: '正在检查查询是否可以执行', explaining: '正在整理数据与结果说明' } as Record<string, string>)[props.run.status] || '正在获取本次查询所需信息'
})
function answer(id: string, value: string) {
  const next = { ...props.answers, [id]: value }
  const visible = new Set(visibleQuestions(props.run.requirement_draft?.clarifications || [], next).map(q => q.id))
  for (const key of Object.keys(next)) if (!visible.has(key)) delete next[key]
  emit('update:answers', next)
}
function openRecord() { recordOpen.value = true }
</script>

<template>
  <div class="task-detail">
    <header class="task-header">
      <div class="task-heading">
        <div class="task-eyebrow">{{ domainName }}<span>/</span>查询任务</div>
        <h1>{{ title || '本次查询' }}</h1>
        <TaskStatus :run="run" :connection="connection" :last-signal="lastSignal"/>
      </div>
      <div class="task-header-actions"><button class="record-button" aria-label="查看本次查询的执行记录" :aria-expanded="recordOpen" @click="openRecord"><HistoryIcon/><span>执行记录</span></button><t-button v-if="active" size="small" variant="text" @click="emit('cancel')">取消查询</t-button></div>
    </header>
    <div class="task-content">
      <section class="request-section detail-section"><h2 class="block-title"><span>01</span>原始需求</h2><p class="request-copy">{{ originalRequest || run.continuation?.question_history?.[0] || run.question }}</p></section>
      <section class="understanding-section detail-section">
        <h2 class="block-title"><span>02</span>理解后的需求<span v-if="run.requirement" class="basis-state">已确认口径</span></h2>
        <p v-if="basis" class="basis-copy">{{ basis }}</p><p v-else class="block-placeholder">{{ run.status === 'failed' ? '本次尚未形成完整的需求理解。' : '正在整理需求与业务口径。' }}</p>
        <details v-if="statisticsRules.length" :key="run.id" class="statistics-rules">
          <summary><ChevronRightIcon aria-hidden="true"/>查看统计规则</summary>
          <ul><li v-for="(rule, index) in statisticsRules" :key="index">{{ rule }}</li></ul>
        </details>

      <section v-if="run.status === 'clarifying'" class="clarification-section">
        <div class="section-heading"><h2>{{ configurationOnly ? '本次查询需要补齐配置' : '请确认以下业务口径' }}</h2><span v-if="questions.length">{{ questions.length }} 项待确认</span></div>
        <p class="section-description">{{ configurationOnly ? '所需关联或映射暂不完整，已有需求与回答会保留。' : '这些选择会影响查询结果，请一并确认。' }}</p>
        <div v-for="(q, index) in questions" :key="q.id" class="business-question" :data-question-id="q.id">
          <h3><span>{{ String(index + 1).padStart(2, '0') }}</span>{{ q.question }}</h3><p>{{ q.reason }}</p>
          <div v-if="q.options.length" class="business-options"><button v-for="o in q.options" :key="o.id" :disabled="loading" :aria-pressed="answers[q.id] === o.value" @click="answer(q.id, o.value)"><span class="choice-marker"></span><div><strong>{{ o.label }}</strong><small>{{ o.description }}</small></div></button></div>
          <textarea v-if="q.allow_free_text" :value="freeAnswerText(q, answers[q.id])" @input="answer(q.id, ($event.target as HTMLTextAreaElement).value)" :aria-label="`补充：${q.question}`" :disabled="loading" rows="2" :placeholder="q.options.length ? '选项不合适？直接描述您的想法也可以。' : '请补充您的想法。'"/>
        </div>
        <div v-if="gaps.length" class="configuration-notice"><strong>需要补齐的配置</strong><p v-for="(gap, index) in gaps" :key="index">{{ gap.summary }}</p><button class="inline-record" @click="openRecord">查看配置要求 <ChevronRightIcon/></button></div>
        <div class="clarification-footer"><p>{{ configurationOnly ? '配置更新后可继续检查当前需求。' : '确认后，按您的选择继续查询。' }}</p><t-button v-if="configurationOnly" theme="primary" :loading="loading" @click="emit('retry')">配置已更新，重新检查</t-button><t-button v-else theme="primary" :disabled="!ready" :loading="loading" @click="emit('confirm')">确认并继续查询</t-button></div>
      </section>

      <section v-else-if="active" class="running-section" aria-live="polite"><div class="running-mark"></div><div><h2>{{ currentStep }}</h2><p>您可以留在当前页面查看进度，也可以稍后从左侧重新打开这条查询。</p></div></section>

      <section v-else-if="run.status === 'failed'" class="failure-section"><div class="failure-symbol">!</div><div class="failure-content"><h2>{{ run.delivery?.kind === 'cancelled' ? '本次查询已取消' : '查询失败' }}</h2><p>{{ businessFailure(run) }}</p><div v-if="failedSQL" class="failed-sql"><strong>失败 SQL：第 {{ failedSQL.step_index }} 条<span v-if="failedSQL.data_source_label"> · {{ failedSQL.data_source_label }}</span></strong><p>{{ failedSQL.purpose }}</p><pre>{{ failedSQL.sql }}</pre></div><div class="failure-actions"><t-button v-if="run.delivery?.kind !== 'cancelled'" theme="primary" :loading="loading" @click="emit('retry')">重试本次查询</t-button><button class="inline-record" @click="openRecord">查看执行记录 <ChevronRightIcon/></button></div></div></section>

      </section>
      <section class="query-results detail-section">
        <h2 class="block-title"><span>03</span>查询结果</h2>
        <template v-if="run.result">
        <div class="section-heading"><h2>{{ run.result.row_count ? `已返回 ${run.result.row_count.toLocaleString()} 条结果` : '没有符合条件的记录' }}</h2><span v-if="run.result.rows.length > 1000">预览前 1,000 条</span></div>
        <p v-if="run.result.truncated" class="result-limit">结果已达到本次返回上限，可能还有符合条件的记录。可在执行记录中查看查询限制。</p>
        <div v-if="run.result.row_count" ref="resultTable" class="result-table"><table><thead><tr><th v-for="(column, index) in run.result.columns" :key="column">{{ labels[column] || `结果 ${index + 1}` }}</th></tr></thead><tbody><tr v-for="(row, index) in pageRows" :key="`${resultPage}-${index}`"><td v-for="(cell, ci) in row" :key="ci">{{ displayValue(cell) }}</td></tr></tbody></table></div>
        <p v-else class="empty-result">查询已完成，当前条件下没有匹配数据。</p>
        <nav v-if="previewRows.length" class="result-pagination" aria-label="查询结果分页">
          <div class="result-pagination-info"><span aria-live="polite">第 {{ previewStart }}–{{ previewEnd }} 条 / 共 {{ previewRows.length.toLocaleString() }} 条</span><label>每页 <select :value="resultPageSize" aria-label="每页显示行数" @change="changeResultPageSize"><option :value="10">10 行</option><option :value="20">20 行</option><option :value="50">50 行</option></select></label></div>
          <t-pagination :current="resultPage" :page-size="resultPageSize" :total="previewRows.length" :show-page-size="false" :total-content="false" :max-page-btn="5" :show-first-and-last-page-btn="true" show-jumper size="small" @current-change="changeResultPage"/>
        </nav>
        </template>
        <div v-else-if="run.status === 'completed'" class="sql-only"><p>本次查询语句已生成，未保存执行结果。可在执行记录中查看查询语句及校验状态。</p><button class="inline-record" @click="openRecord">查看查询语句 <ChevronRightIcon/></button></div>
        <p v-else class="block-placeholder">{{ run.status === 'failed' ? '本次查询尚未取得结果。' : '查询完成后将在这里展示结果。' }}</p>
      </section>
      <section class="explanation-section detail-section">
        <h2 class="block-title"><span>04</span>结果说明</h2>
        <p v-if="run.result && explanation && run.status !== 'failed'" class="explanation-copy">{{ explanation }}</p>
        <p v-else class="block-placeholder">{{ run.result ? '本次没有额外的结果说明。' : '查询完成后展示结果说明。' }}</p>
      </section>
    </div>
    <ExecutionRecord v-model:visible="recordOpen" :run="run" :gaps="gaps" :format-text="readable"/>
  </div>
</template>

<style scoped>
.task-detail{height:100%;display:flex;flex-direction:column;background:var(--semora-page);min-width:0}
.failure-content{min-width:0;flex:1}.failed-sql{margin:14px 0;padding:12px;background:var(--semora-page);border:1px solid var(--semora-line);border-radius:6px}.failed-sql strong{font-size:12px}.failed-sql pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px;line-height:1.7;margin:10px 0 0}
.task-header{width:100%;max-width:1184px;align-self:center;padding:28px 32px 22px;display:flex;justify-content:space-between;align-items:flex-start;gap:24px;flex-shrink:0}
.task-heading{min-width:0;flex:1}
.task-eyebrow{font-size:11px;color:var(--semora-muted);display:flex;gap:9px;align-items:center;letter-spacing:.3px}
.task-eyebrow span{opacity:.5}
.task-heading h1{font-size:22px;font-weight:600;line-height:1.5;letter-spacing:.2px;margin:8px 0 0;display:-webkit-box;-webkit-box-orient:vertical;-webkit-line-clamp:2;overflow:hidden}
.task-header-actions{display:flex;align-items:center;gap:8px;flex-shrink:0;padding-top:3px}
.record-button{display:flex;align-items:center;gap:7px;border:1px solid var(--semora-line);background:transparent;color:var(--semora-ink);border-radius:6px;padding:9px 12px;cursor:pointer;font-size:12px;transition:background .15s}
.record-button:hover{background:var(--semora-card)}
.record-button svg{font-size:16px}
.task-content{flex:1;min-height:0;overflow:auto;display:flex;flex-direction:column;gap:16px;padding:0 32px 36px;scrollbar-width:thin;scrollbar-gutter:stable both-edges}
.detail-section{flex-shrink:0;width:100%;max-width:1120px;margin:0 auto;padding:23px 26px;background:var(--semora-card);border:1px solid var(--semora-line);border-radius:10px;box-shadow:0 3px 14px rgba(17,36,62,.025)}
.block-title{display:flex;align-items:center;gap:10px;font-size:14px;font-weight:600;margin:0 0 14px;letter-spacing:.2px}
.block-title>span:first-child{order:2;margin-left:auto;font-size:10px;color:var(--semora-muted);opacity:.7;font-weight:400;font-variant-numeric:tabular-nums}
.block-title .basis-state{order:1;color:var(--semora-green-dark);font-size:11px;font-weight:400;letter-spacing:0;margin-left:4px}
.request-copy{font-size:14px;line-height:1.95;margin:0;color:var(--semora-ink);white-space:pre-wrap;overflow-wrap:anywhere}
.basis-copy,.explanation-copy{font-size:13px;line-height:1.95;white-space:pre-wrap;overflow-wrap:anywhere;margin:0;color:var(--semora-ink)}
.statistics-rules{margin-top:16px;padding-top:12px;border-top:1px solid var(--semora-line)}
.statistics-rules summary{display:flex;align-items:center;gap:5px;width:fit-content;list-style:none;font-size:12px;color:var(--semora-green-dark);cursor:pointer}
.statistics-rules summary::-webkit-details-marker{display:none}
.statistics-rules summary svg{font-size:14px;flex-shrink:0}
.statistics-rules[open] summary svg{transform:rotate(90deg)}
.statistics-rules ul{margin:11px 0 0;padding-left:22px;color:var(--semora-muted);font-size:13px;line-height:1.95}
.statistics-rules li{padding-left:2px;margin:5px 0;white-space:pre-wrap;overflow-wrap:anywhere}
.explanation-copy{color:var(--semora-muted)}
.block-placeholder{font-size:12px;color:var(--semora-muted);line-height:1.9;margin:0}
.section-heading{display:flex;justify-content:space-between;align-items:center;gap:18px}
.section-heading h2,.running-section h2,.failure-section h2{font-size:14px;font-weight:600;margin:0}
.section-heading>span{font-size:12px;color:var(--semora-muted);white-space:nowrap}
.section-description{font-size:13px;color:var(--semora-muted);margin:7px 0 2px;line-height:1.8}
.clarification-section{border-top:1px solid var(--semora-line);margin-top:20px;padding-top:20px}
.business-question{padding:23px 0;border-bottom:1px solid var(--semora-line)}
.business-question h3{font-size:14px;line-height:1.8;font-weight:600;margin:0;display:flex;gap:10px}
.business-question h3>span{font-size:11px;font-variant-numeric:tabular-nums;color:var(--semora-green-dark);margin-top:3px}
.business-question>p{font-size:12px;line-height:1.9;color:var(--semora-muted);margin:7px 0 12px}
.business-options{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}
.business-options button{display:flex;align-items:flex-start;gap:10px;text-align:left;border:1px solid var(--semora-line);background:var(--semora-card);border-radius:6px;padding:13px 14px;color:var(--semora-ink);cursor:pointer;transition:background .15s,border-color .15s}
.business-options button:hover{border-color:var(--semora-green)}
.business-options button[aria-pressed=true]{border-color:var(--semora-green);background:var(--semora-green-soft)}
.choice-marker{width:14px;height:14px;margin-top:4px;border:1px solid var(--semora-line);border-radius:50%;flex-shrink:0}
.business-options button[aria-pressed=true] .choice-marker{border:4px solid var(--semora-green);background:var(--semora-card)}
.business-options strong{display:block;font-size:13px;font-weight:500;line-height:1.8}
.business-options small{display:block;font-size:12px;line-height:1.7;color:var(--semora-muted);margin-top:3px}
.business-options button:disabled{opacity:.65;cursor:default}
.business-question textarea{width:100%;resize:vertical;min-height:64px;border:1px solid var(--semora-line);background:var(--semora-card);border-radius:6px;padding:11px 12px;margin-top:11px;font-size:13px;line-height:1.8;color:var(--semora-ink);font-family:inherit}
.business-question textarea:focus{outline:2px solid var(--semora-green-soft);border-color:var(--semora-green)}
.clarification-footer{display:flex;align-items:center;justify-content:space-between;gap:20px;padding-top:19px}
.clarification-footer p{font-size:12px;color:var(--semora-muted);margin:0}
.configuration-notice{padding:17px 0;font-size:13px;line-height:1.8}
.configuration-notice p{color:var(--semora-muted)}
.inline-record{display:inline-flex;align-items:center;gap:4px;border:0;background:transparent;color:var(--semora-green-dark);padding:0;font-size:12px;cursor:pointer;white-space:nowrap}
.running-section{display:flex;align-items:flex-start;gap:15px;border-top:1px solid var(--semora-line);margin-top:18px;padding-top:20px}
.running-section p,.sql-only p{font-size:13px;line-height:1.9;color:var(--semora-muted);margin:8px 0 0}
.running-mark{width:10px;height:10px;border-radius:50%;background:var(--semora-green);box-shadow:0 0 0 5px var(--semora-green-soft);flex-shrink:0;margin-top:7px}
.failure-section{display:flex;gap:14px;margin-top:18px;padding-top:20px;border-top:1px solid var(--semora-line)}
.failure-symbol{display:grid;place-items:center;width:24px;height:24px;border-radius:50%;background:color-mix(in srgb,#e34d59 10%,var(--semora-card));color:#c24951;font-weight:600;flex-shrink:0}
.failure-section p{font-size:13px;line-height:1.9;color:var(--semora-muted);margin:9px 0 17px}
.failure-actions{display:flex;gap:20px;align-items:center}
.query-results>.section-heading h2{font-size:12px;font-weight:400;color:var(--semora-muted)}
.result-table{overflow:auto;margin-top:17px;max-height:520px;border-radius:6px;overscroll-behavior:contain;scrollbar-width:thin;scrollbar-gutter:stable}
.result-table table{border-collapse:separate;border-spacing:0;width:100%;font-size:12px}
.result-table th{position:sticky;top:0;background:var(--semora-sidebar);color:var(--semora-muted);font-weight:500;z-index:1}
.result-table th,.result-table td{padding:12px 15px;text-align:left;white-space:nowrap;border-bottom:1px solid var(--semora-line);font-variant-numeric:tabular-nums}
.result-table tbody tr:last-child td{border-bottom:0}
.result-table tbody tr:hover{background:var(--semora-sidebar)}
.result-pagination{display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:12px 18px;margin-top:16px;padding-top:14px;border-top:1px solid var(--semora-line)}
.result-pagination-info{display:flex;align-items:center;flex-wrap:wrap;gap:12px 18px;font-size:11px;color:var(--semora-muted);font-variant-numeric:tabular-nums}
.result-pagination-info label{display:flex;align-items:center;gap:7px;white-space:nowrap}
.result-pagination-info select{padding:4px 6px;border:1px solid var(--semora-line);border-radius:5px;background:var(--semora-card);color:var(--semora-ink);font:inherit;cursor:pointer}
.result-pagination :deep(.t-pagination){width:auto;flex-wrap:wrap;row-gap:8px}
.empty-result{padding:20px 0;color:var(--semora-muted);font-size:13px}
.result-limit{font-size:12px;line-height:1.8;color:#9c691d;background:transparent}
.sql-only p{margin:0}
.sql-only .inline-record{margin-top:12px}
@media(max-width:1000px){
  .task-header{padding:24px 24px 20px;gap:16px}
  .task-heading h1{font-size:20px}
  .task-content{padding:0 24px 30px;gap:14px}
  .detail-section{padding:21px}
  .business-options{grid-template-columns:1fr}
  .record-button span{display:none}
  .clarification-footer{align-items:flex-start;flex-direction:column}
  .task-header-actions{gap:3px}
}
</style>
