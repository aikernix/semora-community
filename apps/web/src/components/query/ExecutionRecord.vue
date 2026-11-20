<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import type { QueryRun, QueryConfigurationGap } from '../../api'
import { analysisOutcomes } from '../../analysisTimeline'
import { sqlFailure } from '../../queryPresentation'

const props = defineProps<{ visible: boolean; run: QueryRun; gaps: QueryConfigurationGap[]; formatText?: (value:string)=>string }>()
const emit = defineEmits<{ 'update:visible': [value: boolean] }>()
const tab = ref('process')
watch(() => props.run.id, () => { tab.value = 'process' })
const readable=(value:string)=>props.formatText?.(value)||value
const outcomes = computed(() => analysisOutcomes(props.run.events || []).map(item=>({...item,
  summary:readable(item.summary),details:item.details.map(readable),changes:item.changes?.map(change=>({...change,
    before:typeof change.before==='string'?readable(change.before):change.before,
    after:typeof change.after==='string'?readable(change.after):change.after}))})))
const sqlSteps = computed<Array<{ name: string; purpose: string; sql: string; artifact: any; inputs: any[]; parameters?: Record<string, unknown> }>>(() => {
  const steps = props.run.plan?.sql_program?.steps
  if (steps?.length) return steps.map((step: any) => ({ ...step, artifact: props.run.source_artifacts?.[step.name] || (steps.length === 1 ? props.run.sql : null) }))
  return props.run.sql ? [{ name: 'query', purpose: '本次查询', sql: props.run.sql.semantic_sql, artifact: props.run.sql, inputs: [] }] : []
})
const evidence = computed(() => [...new Map((props.run.evidence || []).map((e: any) => [JSON.stringify([e.knowledge_base_id, e.document_id, e.chunk_id, e.revision, e.locator]), e])).values()])
const labels = computed(() => props.run.plan?.sql_program?.steps?.at(-1)?.output_labels || {})
const failure = computed(() => sqlFailure(props.run))
function stepStatus(name: string, artifact: any) {
  if (failure.value && props.run.plan?.sql_program?.steps?.[failure.value.step_index - 1]?.name === name) return `执行失败 · ${failure.value.message}`
  const event = [...props.run.events].reverse().find(e => ['source.step.completed', 'source.step.skipped'].includes(e.event) && e.data.step === name)
  if (event) return event.event === 'source.step.skipped' ? '已跳过 · 前置集合为空' : `已执行 · 返回 ${event.data.row_count ?? 0} 条`
  if (props.run.result && sqlSteps.value.at(-1)?.name === name) return `已执行 · 返回 ${props.run.result.row_count} 条`
  return artifact?.compilation?.validation?.state === 'validated' ? '数据库校验通过 · 待执行' : '尚未完成数据库校验'
}
</script>

<template>
  <t-drawer :visible="visible" header="执行记录" :footer="false" size="min(680px, 94vw)" :close-on-overlay-click="true" @close="emit('update:visible', false)">
    <div class="record-context"><span>本次查询</span></div>
    <nav class="record-tabs" aria-label="执行记录内容">
      <button v-for="item in [{ key: 'process', label: '执行过程' }, { key: 'sql', label: '查询语句' }, { key: 'sources', label: '资料与口径' }]" :key="item.key" :aria-pressed="tab === item.key" :class="{ selected: tab === item.key }" @click="tab = item.key">{{ item.label }}</button>
    </nav>
    <section v-if="tab === 'process'" class="record-pane">
      <article v-for="(gap, index) in gaps" :key="index" class="record-error"><strong>{{ readable(gap.summary) }}</strong><p>{{ readable(gap.resolution) }}</p><small>表：{{ gap.objects.map(readable).join('、') }} · 字段：{{ gap.field_refs.map(readable).join('、') }}</small></article>
      <div v-if="run.error" class="record-error"><strong>本次未完成原因</strong><p>{{ readable(run.error) }}</p></div>
      <ol v-if="outcomes.length" class="record-timeline"><li v-for="item in outcomes" :key="item.key"><span class="record-node"></span><div><time class="record-caption">{{ new Date(item.created_at).toLocaleTimeString() }}</time><br/><strong>{{ item.title }}</strong><p>{{ item.summary }}</p><details v-if="item.details.length || item.changes?.length"><summary>查看依据与变化</summary><ul><li v-for="(detail, index) in item.details" :key="index">{{ detail }}</li></ul><div v-for="change in item.changes" :key="change.path"><strong>{{ change.path }}</strong><p>修改前：{{ change.before }}</p><p>修改后：{{ change.after }}</p></div></details></div></li></ol>
      <p v-else class="record-empty">尚未形成新的执行产出。</p>
      <details class="record-secondary"><summary>阶段与时间</summary><ol><li v-for="(event, index) in run.events.filter((event, i, all) => i === 0 || event.status !== all[i - 1].status)" :key="index"><time>{{ new Date(event.created_at).toLocaleString() }}</time> · {{ event.status }}</li></ol></details>
      <details v-if="run.delivery?.warnings?.length" class="record-secondary" open><summary>本次需留意的事项</summary><ul><li v-for="(warning, index) in run.delivery.warnings" :key="index">{{ warning }}</li></ul></details>
    </section>
    <section v-else-if="tab === 'sql'" class="record-pane">
      <p v-if="!sqlSteps.length" class="record-empty">当前还没有生成查询语句。</p>
      <article v-for="(step, index) in sqlSteps" :key="step.name" class="sql-step">
        <div class="sql-step-head"><strong>{{ index + 1 }}. {{ step.purpose }}</strong><span>{{ step.artifact?.dialect || '生成草案' }}</span></div>
        <p class="record-caption">{{ stepStatus(step.name, step.artifact) }}</p>
        <p class="record-caption" v-if="step.artifact?.data_source_id">数据源：{{ step.artifact.compilation?.data_source_label || step.artifact.data_source_id }}</p>
        <pre class="code-block">{{ step.artifact?.physical_sql || step.sql }}</pre>
        <details><summary>参数与步骤依赖</summary><p v-if="!step.artifact">以下为生成草案，尚未编译为目标数据库语句。</p><pre>{{ JSON.stringify(step.artifact?.parameters || step.parameters || {}, null, 2) }}</pre><p v-for="(binding, bi) in step.inputs || []" :key="bi">{{ binding.source_step }} → {{ binding.parameter }} · {{ (binding.source_columns || []).join('、') }}</p></details>
      </article>
      <details v-if="run.result" class="record-secondary"><summary>结果列映射</summary><ul><li v-for="(column, index) in run.result.columns" :key="column">{{ labels[column] || `结果 ${index + 1}` }}：<code>{{ column }}</code></li></ul></details>
    </section>
    <section v-else class="record-pane">
      <article v-if="run.requirement || run.requirement_draft" class="source-item"><h3>本次统计口径</h3><p>{{ run.requirement?.summary || run.requirement_draft?.summary }}</p><ul v-if="run.plan?.assumptions?.length"><li v-for="assumption in run.plan.assumptions" :key="assumption">{{ assumption }}</li></ul></article>
      <article v-for="round in run.alignment_rounds || []" :key="round.key" class="source-item"><h3>您补充的口径</h3><p v-for="(answer, index) in round.answers" :key="index">{{ answer.label }}</p></article>
      <article v-for="(item, index) in evidence" :key="index" class="source-item"><h3>{{ item.title || '业务资料' }}</h3><small>{{ item.locator }} · {{ item.revision }}</small><p>{{ item.summary }}</p></article>
      <p v-if="!evidence.length" class="record-empty">本次没有引用知识库资料。</p>
      <details v-if="run.plan" class="record-secondary"><summary>查看查询计划</summary><p>{{ run.plan.intent }}</p><ol><li v-for="step in run.plan.sql_program?.steps || []" :key="step.name">{{ step.purpose }}</li></ol></details>
    </section>
  </t-drawer>
</template>

<style scoped>
.record-context{display:flex;justify-content:space-between;gap:10px;color:var(--semora-muted);font-size:12px}.record-tabs{display:flex;gap:26px;border-bottom:1px solid var(--semora-line);margin-top:20px}.record-tabs button{border:0;border-bottom:2px solid transparent;background:transparent;color:var(--semora-muted);padding:11px 0;font-size:13px;cursor:pointer}.record-tabs button.selected{color:var(--semora-green-dark);border-bottom-color:var(--semora-green)}.record-pane{padding:22px 0 30px;font-size:13px;line-height:1.8;overflow-wrap:anywhere}.record-timeline{list-style:none;padding:0;margin:0}.record-timeline>li{display:flex;gap:14px;padding-bottom:24px;position:relative}.record-timeline>li:before{content:'';position:absolute;left:4px;top:15px;bottom:0;width:1px;background:var(--semora-line)}.record-timeline>li:last-child:before{display:none}.record-node{width:9px;height:9px;border-radius:50%;background:var(--semora-green);flex-shrink:0;margin-top:8px}.record-pane p{margin:5px 0;color:var(--semora-muted)}.record-pane strong,.record-pane h3{font-size:13px;font-weight:600;color:var(--semora-ink)}.record-pane details{margin-top:10px}.record-pane summary{cursor:pointer;color:var(--semora-green-dark)}.record-secondary{border-top:1px solid var(--semora-line);padding-top:16px;margin-top:20px!important}.record-error{padding:14px;background:color-mix(in srgb,#e34d59 8%,var(--semora-card));border-radius:8px;margin-bottom:22px;white-space:pre-wrap}.record-empty{padding:12px 0;color:var(--semora-muted)}.source-item,.sql-step{padding:0 0 22px;margin:0 0 22px;border-bottom:1px solid var(--semora-line)}.sql-step-head{display:flex;align-items:baseline;gap:12px;justify-content:space-between}.sql-step-head span,.source-item small,.record-caption{color:var(--semora-muted);font-size:12px}.sql-step .code-block{margin-top:14px;font-size:12px}.source-item h3{margin:0}.source-item p{white-space:pre-wrap}pre{white-space:pre-wrap;overflow-wrap:anywhere}
</style>
