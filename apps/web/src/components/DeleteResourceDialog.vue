<script setup lang="ts">
defineProps<{
  kind: '数据源' | '业务域'
  name: string
  busy: boolean
  error: string
}>()
const emit = defineEmits<{ confirm: []; cancel: [] }>()
</script>

<template>
  <t-dialog
    :visible="!!name"
    :header="`删除${kind}`"
    :confirm-btn="{ content: '确认删除', theme: 'danger', loading: busy, disabled: busy }"
    :cancel-btn="{ disabled: busy }"
    :close-btn="!busy"
    :close-on-overlay-click="false"
    :close-on-esc-keydown="!busy"
    @confirm="emit('confirm')"
    @cancel="emit('cancel')"
    @close="emit('cancel')"
  >
    <p>确定删除{{ kind }}「{{ name }}」？删除后将从可用列表移除。</p>
    <p v-if="kind === '数据源'">仍有业务域绑定、历史查询引用或运行任务时，系统会阻止删除。</p>
    <p v-else>历史查询和所绑定的数据源会保留。删除后该业务域会从工作台移除，待确认或待审批的查询无法继续。仍有查询或分析任务在运行时，系统会阻止删除。</p>
    <t-alert v-if="error" theme="error" :message="error" />
  </t-dialog>
</template>
