<script setup lang="ts">
import { computed } from 'vue'
const props=withDefaults(defineProps<{
  sourceTable:string;targetTable:string;sourceFields:string[];targetFields:string[]
  sourceLabel?:string;targetLabel?:string;cardinality:string;status:string;reason?:string
  condition?:string;optional?:boolean;busy?:boolean;canManage?:boolean
}>(),{busy:false,canManage:true})
const emit=defineEmits<{'edit':[];'decision':[action:'approve'|'reject'|'disable'|'enable']}>()
const known=computed(()=>['1:1','1:N','N:1','N:N'].includes(props.cardinality))
const statusLabel=computed(()=>({accepted:'已生效',pending:'待审核',disabled:'已停用'} as Record<string,string>)[props.status]||props.status)
const conditionText=computed(()=>props.condition||props.sourceFields.map((field,index)=>`${props.sourceTable}.${field} = ${props.targetTable}.${props.targetFields[index]}`).join(' AND '))
</script>

<template>
  <article class="relation-card relationship-card">
    <div class="relation-expression"><span><strong>{{sourceTable}}</strong><code>{{sourceFields.join(', ')}}</code><small v-if="sourceLabel">{{sourceLabel}}</small></span><b>{{cardinality}}</b><span><strong>{{targetTable}}</strong><code>{{targetFields.join(', ')}}</code><small v-if="targetLabel">{{targetLabel}}</small></span></div>
    <p v-if="reason" class="relation-reason">{{reason}}</p>

    <div class="relation-meta">
      <details v-if="conditionText" class="relation-condition"><summary>查看完整关联条件</summary><code>{{conditionText}}</code></details>
      <div class="relation-badges"><t-tag :theme="status==='accepted'?'success':status==='pending'?'warning':'default'" variant="light">{{statusLabel}}</t-tag><t-tag v-if="optional" variant="light">可选关联</t-tag></div>
      <div v-if="canManage" class="relation-actions">
        <t-button size="small" v-if="status==='accepted'||status==='pending'||!known" variant="text" :disabled="busy" @click="emit('edit')">编辑</t-button>
        <t-button size="small" v-if="status==='accepted'" variant="text" theme="danger" :disabled="busy" @click="emit('decision','disable')">停用</t-button>
        <template v-else-if="status==='pending'"><small v-if="!known">基数未确定，请先编辑</small><t-button size="small" variant="text" theme="primary" :loading="busy" :disabled="busy||!known" @click="emit('decision','approve')">同意</t-button><t-button size="small" variant="text" theme="danger" :disabled="busy" @click="emit('decision','reject')">拒绝</t-button></template>
        <t-button size="small" v-else variant="text" theme="primary" :loading="busy" :disabled="busy||!known" @click="emit('decision','enable')">启用</t-button>
      </div>
    </div>
  </article>
</template>

<style scoped>
.relation-card.relationship-card {
  border-bottom: 1px solid var(--semora-line);
  padding: 13px 12px 9px;
  min-width: 0;
}
.relation-card.relationship-card:hover { background: #f8fafd; }
.relation-expression {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 44px minmax(0, 1fr);
  gap: 12px;
  align-items: start;
  font-size: 13px;
}
.relation-expression > span { min-width: 0; }
.relation-expression strong, .relation-expression code, .relation-expression small { display: block; overflow-wrap: anywhere; }
.relation-expression code { margin-top: 4px; color: #3f6187; }
.relation-expression small { margin-top: 3px; color: var(--semora-muted); font-size: 12px; }
.relation-expression > b {
  justify-self: center;
  padding: 3px 6px;
  border-radius: 4px;
  background: #edf4fc;
  color: #234b79;
  font-size: 12px;
}
.relation-reason { margin: 8px 0 0; color: var(--semora-muted); font-size: 13px; line-height: 1.6; overflow-wrap: anywhere; }
.relation-meta { display: flex; gap: 6px 14px; align-items: center; flex-wrap: wrap; margin-top: 6px; }
.relation-condition { flex: 1 1 180px; min-width: 0; font-size: 12px; color: var(--semora-muted); }
.relation-condition summary { cursor: pointer; width: fit-content; padding-block: 4px; }
.relation-condition code { display: block; margin-top: 6px; overflow-wrap: anywhere; white-space: pre-wrap; }
.relation-badges, .relation-actions { display: flex; align-items: center; flex-wrap: wrap; gap: 4px; }
.relation-actions { margin-left: auto; }
.relation-actions small { font-size: 12px; color: var(--semora-muted); }
@media (max-width: 600px) {
  .relation-expression { grid-template-columns: minmax(0, 1fr) 38px minmax(0, 1fr); gap: 6px; }
  .relation-meta { gap: 4px 8px; }
  .relation-condition { flex-basis: 100%; }
}
</style>
