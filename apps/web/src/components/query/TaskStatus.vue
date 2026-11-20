<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import type { QueryRun } from '../../api'
import { publicStatus, terminalStatus } from '../../queryPresentation'

const props = defineProps<{ run: QueryRun; connection: string; lastSignal: number }>()
const now = ref(Date.now())
let timer: ReturnType<typeof setInterval>
onMounted(() => { timer = setInterval(() => { now.value = Date.now() }, 1000) })
onUnmounted(() => clearInterval(timer))
const active = computed(() => !terminalStatus(props.run.status))
const stale = computed(() => active.value && props.lastSignal > 0 && now.value - props.lastSignal > 20_000)
const reconnecting = computed(() => active.value && (stale.value || props.connection === 'reconnecting'))
const elapsed = computed(() => {
  const start = Date.parse(props.run.created_at || props.run.events[0]?.created_at || '')
  const end = active.value ? now.value : Date.parse(props.run.events.at(-1)?.created_at || '')
  if (!Number.isFinite(start) || !Number.isFinite(end)) return ''
  const seconds = Math.max(0, Math.floor((end - start) / 1000))
  const units = [Math.floor(seconds / 3600), Math.floor(seconds / 60) % 60, seconds % 60]
  return (units[0] ? units : units.slice(1)).map(v => String(v).padStart(2, '0')).join(':')
})
const signalLabel = computed(() => {
  if (!active.value) {
    if (props.run.status !== 'clarifying') return ''
    const draft = props.run.requirement_draft
    return draft?.technical_gaps?.length && !draft.clarifications.length
      ? '配置补齐后可重新检查' : '完成确认后继续处理'
  }
  if (reconnecting.value) return '连接恢复中 · 正在重新获取本次进度'
  if (props.connection !== 'live') return '正在连接服务'
  const seconds = Math.max(0, Math.floor((now.value - props.lastSignal) / 1000))
  return `实时连接正常 · ${seconds < 2 ? '刚刚' : seconds + '秒前'}收到响应`
})
</script>

<template>
  <div class="task-status" :class="{ active, reconnecting, failed: run.status === 'failed', waiting: run.status === 'clarifying' }">
    <div class="status-heading" role="status" aria-live="polite"><span class="status-symbol"><span v-if="active && !reconnecting" class="status-spinner"></span><span v-else-if="run.status === 'completed'">✓</span><span v-else-if="run.status === 'failed'">!</span><span v-else>●</span></span><strong>{{ publicStatus(run) }}</strong></div>
    <div class="status-meta"><span v-if="elapsed">任务历时 {{ elapsed }}</span><span v-if="signalLabel">{{ signalLabel }}</span></div>
  </div>
</template>

<style scoped>
.task-status{display:flex;align-items:center;flex-wrap:wrap;gap:8px 14px;margin-top:12px;color:var(--semora-green-dark)}
.status-heading{display:flex;align-items:center;gap:6px}
.status-heading strong{font-size:12px;font-weight:500}
.status-symbol{display:grid;place-items:center;width:14px;height:14px;font-size:11px}
.status-spinner{width:12px;height:12px;border:2px solid var(--semora-line);border-top-color:var(--semora-green);border-radius:50%;animation:status-spin 1s linear infinite}
.status-meta{display:flex;gap:8px 14px;flex-wrap:wrap;align-items:center;color:var(--semora-muted);font-size:11px;font-variant-numeric:tabular-nums}
.status-meta>span{border-left:1px solid var(--semora-line);padding-left:14px}
.failed{color:#c24951}
.waiting,.reconnecting{color:#a26a1b}
@keyframes status-spin{to{transform:rotate(360deg)}}
@media(prefers-reduced-motion:reduce){.status-spinner{animation:none}}
</style>
