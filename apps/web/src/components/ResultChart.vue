<script setup lang="ts">
import { computed } from 'vue'

const props = defineProps<{
  columns: string[]
  rows: unknown[][]
}>()

type ChartSeries = {
  column: string
  values: Array<{ label: string; value: number; width: number }>
}

function numericValue(value: unknown): number | null {
  if (typeof value === 'number' && Number.isFinite(value)) return value
  if (typeof value !== 'string' || !value.trim()) return null
  const normalized = value.replaceAll(',', '').replace(/%$/, '').trim()
  const parsed = Number(normalized)
  return Number.isFinite(parsed) ? parsed : null
}

function displayValue(value: number): string {
  return new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 2 }).format(value)
}

function isDimensionColumn(column: string): boolean {
  return /(^id$|_id$|_code$|_no$|name|date|time|branch|客户|机构|分行|日期|时间)/i.test(column)
}

const charts = computed<ChartSeries[]>(() => {
  if (!props.columns.length || !props.rows.length) return []
  const sample = props.rows.slice(0, 12)
  const numericColumns = props.columns
    .map((column, index) => ({
      column,
      index,
      values: sample.map(row => numericValue(row[index])),
    }))
    .filter(item => !isDimensionColumn(item.column) && item.values.some(value => value !== null))
  if (!numericColumns.length) return []
  const dimensionIndex = props.columns.findIndex((column, index) =>
    isDimensionColumn(column) || !numericColumns.some(item => item.index === index),
  )
  const labelIndex = dimensionIndex >= 0 ? dimensionIndex : 0
  return numericColumns.slice(0, 4).map(item => {
    const numbers = item.values.map(value => value ?? 0)
    const maximum = Math.max(...numbers.map(Math.abs), 1)
    return {
      column: item.column,
      values: numbers.map((value, index) => ({
        label: String(sample[index][labelIndex] ?? `第 ${index + 1} 行`),
        value,
        width: Math.max(2, Math.abs(value) / maximum * 100),
      })),
    }
  })
})
</script>

<template>
  <section v-if="charts.length" class="chart-section" aria-label="查询结果图表">
    <div class="chart-heading">
      <strong>自动图表</strong>
      <span>基于已脱敏结果确定性生成 · 最多展示 12 行、4 个数值列</span>
    </div>
    <div class="chart-grid">
      <article v-for="chart in charts" :key="chart.column" class="chart-card">
        <h4>{{ chart.column }}</h4>
        <div class="bars">
          <div v-for="item in chart.values" :key="`${item.label}-${item.value}`" class="bar-row">
            <span class="bar-label" :title="item.label">{{ item.label }}</span>
            <div class="bar-track">
              <i :class="{ negative: item.value < 0 }" :style="{ width: `${item.width}%` }"></i>
            </div>
            <b>{{ displayValue(item.value) }}</b>
          </div>
        </div>
      </article>
    </div>
  </section>
</template>

<style scoped>
.chart-section{border-top:1px solid var(--semora-line);padding:16px 18px;background:color-mix(in srgb,var(--semora-card) 97%,var(--semora-page))}.chart-heading{display:flex;justify-content:space-between;gap:16px;align-items:center;margin-bottom:13px}.chart-heading span{font-size:11px;color:var(--semora-muted)}.chart-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}.chart-card{border:1px solid var(--semora-line);border-radius:9px;background:var(--semora-card);padding:13px;min-width:0}.chart-card h4{font-size:13px;margin:0 0 12px}.bars{display:grid;gap:8px}.bar-row{display:grid;grid-template-columns:90px minmax(80px,1fr) 86px;gap:8px;align-items:center;font-size:11px}.bar-label{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--semora-muted)}.bar-track{height:10px;border-radius:5px;background:var(--semora-page);overflow:hidden}.bar-track i{display:block;height:100%;border-radius:5px;background:linear-gradient(90deg,var(--semora-green),#4bd685)}.bar-track i.negative{background:linear-gradient(90deg,#e34d59,#f58b94)}.bar-row b{text-align:right;font-variant-numeric:tabular-nums;font-size:11px;overflow:hidden;text-overflow:ellipsis}@media(max-width:900px){.chart-grid{grid-template-columns:1fr}.chart-heading{align-items:flex-start;flex-direction:column}.bar-row{grid-template-columns:75px 1fr 72px}}
</style>
