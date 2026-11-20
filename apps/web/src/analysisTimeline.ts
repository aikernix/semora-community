import type { RunEvent } from './api'

export interface AnalysisOutcome {
  key: string; created_at: string; title: string; summary: string; details: string[]
  changes?: { path: string; before: unknown; after: unknown }[]
}
const text = (value: unknown) => typeof value === 'string' ? value : ''
export function displayValue(value: unknown): string {
  if (value === null || value === undefined) return '未设置'
  return typeof value === 'string' ? value : JSON.stringify(value)
}

// Project immutable events into user-visible outputs; never count model calls as findings.
export function analysisOutcomes(events: RunEvent[]): AnalysisOutcome[] {
  const rows: AnalysisOutcome[] = []
  const seen = new Set<string>()
  const goals = new Map<string, string>()
  const add = (event: RunEvent, title: string, summary: string, details: string[] = [], changes?: AnalysisOutcome['changes']) => {
    if (!summary) return
    const signature = JSON.stringify([event.event, event.data.step, event.data.attempt,
      event.data.requirement?.version, title, summary, details, changes])
    // Preserve different conclusions and steps, collapse exact replayed output.
    if (seen.has(signature)) return
    seen.add(signature)
    rows.push({ created_at: event.created_at, key: `${event.created_at}-${rows.length}`, title, summary, details, changes })
  }
  for (const e of events) {
    const d = e.data || {}, o = d.observation || {}
    if (e.event === 'agent.tool.started') { goals.set(d.tool, text(d.analysis_goal)); continue }
    if (e.event === 'agent.tool.completed') {
      if (d.available === false) continue
      if (d.tool === 'search_wiki' && o.count > 0) {
        add(e, '找到相关知识', `检索“${o.keyword || ''}”，找到 ${o.count} 个相关页面`, (o.pages || []).map((p: any) => p.title || p.slug))
      } else if (d.tool === 'read_wiki' && o.title) {
        add(e, '读取知识依据', o.title, [text(o.excerpt), ...(o.source_refs || []).map((s: any) => typeof s === 'string' ? s : s.title || s.document_id || '')].filter(Boolean))
      } else if (d.tool === 'read_object' && o.object) {
        add(e, '取得对象定义', `${o.label || o.object}：${o.field_count} 个字段，${(o.relations || []).length} 条直接关系`, [o.object, ...(o.relations || [])])
      } else if (d.tool === 'find_relation_path' && o.path?.length) {
        add(e, '核对关联路径', o.path.join(' → '), [goals.get(d.tool) || '已找到配置中的关联路径'])
      } else if (d.tool === 'read_schema' && o.tables?.length) {
        add(e, '取得表结构', o.tables.map((t: any) => `${t.name}（${t.column_count} 个字段）`).join('、'))
      } else if (d.tool === 'read_source' && o.chunks?.length) {
        add(e, '取得原文证据', `已读取 ${o.chunks.length} 个原文片段`, o.chunks.map((c: any) => c.excerpt).filter(Boolean))
      }
      continue
    }
    if (e.event === 'knowledge.prepared' && d.knowledge_pages > 0) {
      add(e, '取得辅助定义', `取得 ${d.knowledge_pages} 个相关知识页面，已用于选择本体对象`)
    } else if (e.event === 'query.relations.published') {
      add(e, '自动补齐对应关系', text(d.summary))
    } else if (e.event === 'query.relations.finished' && d.status !== 'published') {
      add(e, '保留关联配置缺口', text(d.summary))
    } else if (e.event === 'query.repair.completed') {
      add(e, '已修正查询方案', d.summary || '候选修订已通过程序检查', (d.changed_paths || []).map((path: string) => `调整 ${path}`))
    } else if (e.event === 'sql.repaired' || e.event === 'sql.semantic.corrected') {
      add(e, '修正 SQL', text(d.summary), [d.step ? `步骤：${d.step}` : ''].filter(Boolean))
    } else if (e.event === 'rehearsal.ready') {
      add(e, '完成查询方案预演', `${d.questions || 0} 项待确认，${d.conditional_questions || 0} 个条件问题，${d.technical_gaps || 0} 项配置缺口`, d.skeleton || [])
    } else if (e.event === 'rehearsal.finalized') {
      add(e, '确认口径已落实到方案', text(d.summary), d.skeleton || [])
    } else if (e.event === 'rehearsal.answers.applied') {
      add(e, '答案已填入预演方案', text(d.summary))
    } else if (e.event === 'rehearsal.configuration_required') {
      add(e, '需要补齐具体配置', text(d.summary), (d.gaps || []).map((g: any) => g.summary))
    } else if (e.event === 'plan.candidate.validated') {
      add(e, d.revision ? '修订查询方案' : '形成候选方案', text(d.summary),
        [...(d.conclusions || []), ...(d.additional_changes ? [`另有 ${d.additional_changes} 处变化，完整记录见审计。`] : [])], d.changes || [])
    } else if (e.event === 'plan.preflight.completed' || e.event === 'review.completed') {
      const review = d.review || d
      if (review.decision) add(e, review.decision === 'approved' ? '复核通过' : '复核发现问题', text(review.rationale),
        [...(review.findings?.length ? review.findings.map((f: any) => `${f.message}；修复要求：${f.acceptance}`) : review.issues || []),
         ...(review.resolutions || []).map((r: any) => `${r.status === 'resolved' ? '已解决' : '未解决'} ${r.finding_id}：${r.rationale}`)])
    } else if (e.event === 'alignment.required') {
      const req = d.requirement || {}
      const questions = (req.clarifications || []).map((q: any) => q.question)
      if (questions.length) add(e, '需要你补充', questions.join('；'), (req.clarifications || []).map((q: any) => q.reason).filter(Boolean))
    } else if (e.event === 'result.summary.completed') {
      add(e, d.mode === 'model' ? '形成业务结论' : '整理基本结果说明', text(d.summary))
    } else if (e.event === 'source.step.retry') {
      add(e, '重试超时查询', text(d.summary))
    } else if (e.event === 'source.step.completed') {
      add(e, '完成数据查询', `${text(d.summary) || '已取得所需信息'}${d.row_count !== undefined ? `，找到 ${d.row_count} 条记录` : ''}`)
    } else if (e.event === 'plan.revision.stalled') {
      add(e, '修订未取得进展', text(d.summary), d.finding_ids || [])
    }
  }
  return rows
}
