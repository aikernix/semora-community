<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { api, type SystemSettings } from '../api'
import { hasRole } from '../auth'

const modelForm = ref({ base_url: '', model: '', api_key: '' })
const modelKeyConfigured = ref(false)
const modelBusy = ref(false)
const modelMessage = ref('')
const modelError = ref('')
const executionFields = [
  { key: 'query_timeout_seconds', label: '单条 SQL 执行超时', unit: '秒' },
  { key: 'explain_timeout_seconds', label: 'EXPLAIN 预检超时', unit: '秒' },
  { key: 'max_result_rows', label: '最大返回行数', unit: '行' },
  { key: 'max_result_mb', label: '最大返回数据量', unit: 'MiB' },
] as const
const execution = ref<SystemSettings>()
const executionBusy = ref(false)
const executionMessage = ref('')
const executionError = ref('')

async function loadExecution() {
  try {
    execution.value = (await api.get<SystemSettings>('/settings')).data
  } catch {
    executionError.value = '读取查询执行设置失败。'
  }
}

async function saveExecution() {
  const settings = execution.value
  if (!settings || !hasRole('admin')) return
  executionMessage.value = ''
  executionError.value = ''
  for (const { key, label } of executionFields) {
    const value = settings[key]
    if (!Number.isInteger(value) || value < 1) {
      executionError.value = `${label}须为不小于 1 的整数。`
      return
    }
  }
  executionBusy.value = true
  try {
    const { data } = await api.put<SystemSettings>('/settings', {
      ...settings,
      preview_rows: Math.min(settings.preview_rows, settings.max_result_rows),
    })
    execution.value = data
    executionMessage.value = '已保存，新查询将使用这些设置。'
  } catch (error: any) {
    const detail = error?.response?.data?.detail
    executionError.value = typeof detail === 'string' ? detail : '保存查询执行设置失败。'
  } finally {
    executionBusy.value = false
  }
}

async function loadModel() {
  const { data } = await api.get('/settings/model')
  modelForm.value.base_url = data.base_url
  modelForm.value.model = data.model
  modelKeyConfigured.value = Boolean(data.api_key_configured)
}
async function configureModel(testOnly: boolean) {
  modelBusy.value = true
  modelMessage.value = ''; modelError.value = ''
  try {
    if (testOnly) {
      await api.post('/settings/model/test', modelForm.value, { timeout: 60_000 })
      modelMessage.value = '连接成功，模型支持结构化 JSON 输出和 Agent 工具调用。请保存配置以启用。'
    } else {
      const { data } = await api.put('/settings/model', modelForm.value)
      modelKeyConfigured.value = Boolean(data.api_key_configured)
      modelForm.value.api_key = ''
      modelMessage.value = '模型配置已保存并立即生效。API Key 不会返回浏览器。'
    }
  } catch (error: any) { modelError.value = error?.response?.data?.detail || '模型操作失败' }
  finally { modelBusy.value = false }
}

onMounted(() => {
  void loadModel()
  void loadExecution()
})
</script>

<template>
  <div class="page management-page settings-page">
    <div class="page-header">
      <div>
        <h1>系统设置</h1>
        <p>配置基础模型和查询执行限制。</p>
      </div>
    </div>

    <section id="model" class="settings-section model-config">
      <div class="settings-intro">
        <h2>基础模型配置</h2>
        <p class="hint">用于需求理解、逐轮澄清、知识研究和 SQL 说明。请配置支持结构化 JSON 输出的模型。</p>
      </div>
      <div class="form">
        <div class="model-fields">
          <label>API 地址（OpenAI 兼容）<t-input v-model="modelForm.base_url" placeholder="https://your-provider.example/v1" /></label>
          <label>模型名称<t-input v-model="modelForm.model" placeholder="填写服务商提供的模型 ID" /></label>
          <label>API Key<t-input v-model="modelForm.api_key" type="password" autocomplete="new-password" :placeholder="modelKeyConfigured ? '已保存；留空保留原 Key' : '输入 API Key'" /></label>
        </div>
        <t-alert v-if="modelError" theme="error" :message="modelError" />
        <t-alert v-if="modelMessage" theme="success" :message="modelMessage" />
        <div class="model-actions"><t-button variant="outline" :loading="modelBusy" @click="configureModel(true)">测试连接</t-button><t-button theme="primary" :loading="modelBusy" @click="configureModel(false)">保存模型配置</t-button></div>
      </div>
    </section>

    <section id="query-execution" class="settings-section">
      <div class="settings-intro">
        <h2>查询执行设置</h2>
        <p class="hint">超时按单条 SQL 或单次预检计算。超过行数上限时结果会截断，超过数据量上限时查询会报错。</p>
      </div>
      <form class="form" @submit.prevent="saveExecution">
        <div v-if="execution" class="execution-fields">
          <label v-for="{ key, label, unit } in executionFields" :key="key">
            {{ label }}
            <span class="number-field">
              <input v-model.number="execution[key]" :aria-label="label" :aria-description="unit" type="number"
                     min="1" step="1" required
                     :disabled="!hasRole('admin') || executionBusy" />
              <span aria-hidden="true">{{ unit }}</span>
            </span>
          </label>
        </div>
        <t-alert v-if="executionError" theme="error" :message="executionError" />
        <t-alert v-if="executionMessage" theme="success" :message="executionMessage" />
        <div v-if="hasRole('admin')" class="model-actions">
          <t-button type="submit" theme="primary" :loading="executionBusy"
                    :disabled="!execution">保存查询执行设置</t-button>
        </div>
      </form>
    </section>

  </div>
</template>

<style scoped>
.settings-page {
  --management-blue: #0c52a4;
  font-family: Arial, "PingFang SC", "Microsoft YaHei", sans-serif;
  padding-inline: clamp(24px, 2.5vw, 40px);
}
.settings-page > .page-header { margin-bottom: 30px; }
.settings-page :deep(.t-button), .settings-page :deep(.t-input__inner) { font-family: inherit; }
.settings-section {
  display: grid;
  grid-template-columns: minmax(160px, 210px) minmax(0, 720px);
  gap: 32px;
  max-width: 980px;
  padding: 30px 0;
  border-top: 1px solid var(--semora-line);
}
.model-config { border-top: 0; padding-top: 4px; }
.settings-intro h2 { margin: 0; font-size: 20px; line-height: 1.4; }
.settings-intro .hint { margin: 8px 0 0; font-size: 13px; line-height: 1.7; color: var(--semora-muted); }
.model-fields, .execution-fields { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 18px 20px; }
.model-fields label:first-child { grid-column: 1 / -1; }
.form label { display: grid; gap: 7px; font-size: 14px; }
.form .t-alert { margin-top: 12px; }
.execution-fields input {
  display: block;
  width: 100%;
  height: 36px;
  padding: 0 62px 0 12px;
  border: 1px solid var(--td-border-level-2-color);
  border-radius: var(--td-radius-default);
  background: var(--td-bg-color-container);
  color: var(--td-text-color-primary);
}
.execution-fields input:focus-visible { outline: 2px solid var(--td-brand-color); outline-offset: 1px; }
.number-field { position: relative; }
.number-field > span { position: absolute; inset-inline-end: 12px; top: 50%; transform: translateY(-50%); color: var(--semora-muted); pointer-events: none; font-size: 12px; }
.model-actions { display: flex; flex-wrap: wrap; justify-content: flex-end; gap: 8px; margin-top: 16px; }
@media (max-width: 760px) {
  .settings-section { grid-template-columns: 1fr; gap: 18px; }
}
@media (max-width: 480px) {
  .model-fields, .execution-fields { grid-template-columns: 1fr; }
}
</style>
