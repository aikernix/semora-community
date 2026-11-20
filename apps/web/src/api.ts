import axios from 'axios'
import { authRequired, keycloak, refreshToken } from './auth'

export const api = axios.create({ baseURL: '/api/v1', timeout: 40_000 })

api.interceptors.request.use(async config => {
  const token = await refreshToken()
  if (token) config.headers.Authorization = `Bearer ${token}`
  return config
})

api.interceptors.response.use(undefined, async error => {
  if (authRequired && error?.response?.status === 401 && keycloak.authenticated) {
    await keycloak.login()
  }
  return Promise.reject(error)
})

export type RunStatus = 'received' | 'retrieving' | 'understanding' | 'aligning' | 'clarifying' | 'aligned' | 'planning' | 'compiling' | 'gating' | 'reviewing' | 'awaiting_approval' | 'executing' | 'explaining' | 'completed' | 'failed'

export interface ClarificationOption { id: string; label: string; value: string; description: string }
export interface ClarificationQuestion {
  id: string
  field: string
  question: string
  reason: string
  options: ClarificationOption[]
  allow_free_text: boolean
  blocking: boolean
  depends_on?: Array<{ question_id: string; values: string[] }>
}
export interface QueryConfigurationGap {
  kind: 'relation' | 'field' | 'definition' | 'capability'
  summary: string
  required_for: string
  objects: string[]
  field_refs: string[]
  evidence_refs: string[]
  resolution: string
  depends_on?: Array<{ question_id: string; values: string[] }>
}
export interface RequirementDraft {
  clarification_stage?: 'direction' | 'business'
  clarification_round?: number
  technical_gaps?: QueryConfigurationGap[]
  source_steps?: any[]
  version: number
  summary: string
  analysis_steps: string[]
  subject: string
  metrics?: string[]
  dimensions?: string[]
  filters: Array<{ field: string; operator: string; value: unknown }>
  time_semantics?: string
  grain?: string
  data_scope: Record<string, unknown>
  aggregation_rules: string[]
  stages?: Array<{ name: string; label?: string; source: string; group_by: string[]; join_type: 'left' | 'inner'; measures: Array<{ name: string; label?: string; function: string; field?: string; filters?: Array<{ field: string; operator: string; value: unknown }> }>; filters?: Array<{ field: string; operator: string; value: unknown }> }>
  output_fields: string[]
  assumptions: string[]
  confidence: Record<string, number>
  clarifications: ClarificationQuestion[]
  status: 'draft' | 'awaiting_clarification' | 'ready'
}

export interface RunEvent { event: string; status: RunStatus; data: Record<string, any>; created_at: string }
export interface KnowledgeResearchStep { kind: string; tool: string; summary: string; status: string }
export interface KnowledgeResearchResult {
  provider: string
  agent_id: string
  session_id: string
  answer: string
  steps: KnowledgeResearchStep[]
  evidence: any[]
}
export interface QueryRun {
  created_at?: string
  query_rehearsal?: { summary: string; target_state: 'known' | 'needs_direction'; skeleton: string[]; questions: ClarificationQuestion[]; technical_gaps: QueryConfigurationGap[]; confidence?: {level: 'high' | 'medium' | 'low'; reason: string} }
  delivery?: { kind: 'sql' | 'result' | 'clarification' | 'blocked' | 'cancelled' | 'interrupted'; title: string; summary: string; warnings: string[]; next_action: string; result_verified: boolean }
  delivery_warnings?: string[]
  source_artifacts?: Record<string, any>
  id: string
  session_id: string
  domain: string
  question: string
  message_mode?: 'auto' | 'continue' | 'new'
  continuation?: { source_run_id: string; root_run_id: string; question_history: string[]; model_round_offset: number; role_rounds: Record<string, number>; failed_stage: string }
  status: RunStatus
  events: RunEvent[]
  evidence: any[]
  knowledge_research?: KnowledgeResearchResult
  ontology?: any
  requirement_draft?: RequirementDraft
  alignment_rounds?: Array<{ key: string; answers: Array<{ field: string; label: string; value: string }>; draft: RequirementDraft }>
  requirement?: any
  plan?: any
  sql?: any
  sql_explanation?: string
  gate?: any
  review?: any
  result_review?: any
  approval?: any
  result?: { columns: string[]; rows: any[][]; row_count: number; duration_ms: number; truncated: boolean }
  answer?: string
  error?: string
}

export interface QuerySession {
  id: string
  title: string
  domain: string
  created_at: string
  run_count: number
  latest_status?: RunStatus | null
  requirement_form?: Record<string, string>
}

export interface SystemSettings {
  query_timeout_seconds: number
  explain_timeout_seconds: number
  max_result_rows: number
  max_result_mb: number
  preview_rows: number
  version: number
  policy_version: string
  updated_at: string
  updated_by: string
}

