import type { QueryRun, RunEvent } from './api'

export const terminalStatus = (status: string) => ['completed', 'failed', 'clarifying', 'awaiting_approval'].includes(status)

export function mergeRunEvent(run: QueryRun, sequence: number, event: RunEvent) {
  if (!Number.isInteger(sequence) || sequence < 1 || sequence <= run.events.length) return 'ignored'
  if (sequence !== run.events.length + 1) return 'gap'
  run.events.push(event)
  run.status = event.status
  return 'applied'
}

export function publicStatus(run: QueryRun) {
  if (run.status === 'failed') return run.delivery?.kind === 'cancelled' ? '查询已取消' : '查询失败'
  if (run.status === 'clarifying') return run.requirement_draft?.technical_gaps?.length && !run.requirement_draft.clarifications.length ? '等待配置更新' : '等待您确认'
  if (run.status === 'completed') return run.result ? '查询已完成' : '查询语句已生成'
  return ({ received: '已收到您的需求', retrieving: '正在查阅业务资料', understanding: '正在理解您的需求', aligning: '正在整理业务口径', aligned: '正在准备查询', planning: '正在准备查询', compiling: '正在检查查询', executing: '正在获取所需数据', explaining: '正在整理查询结果' } as Record<string, string>)[run.status] || '正在处理本次查询'
}

// Business milestones are a projection, not a copy of the diagnostic event log.
// A revised stage updates its existing row; source operations stay in the drawer.
export function businessOutcomes(events: RunEvent[]) {
  const rows = new Map<string, { key: string; title: string; summary: string }>()
  const put = (key: string, title: string, summary: string) => rows.set(key, { key, title, summary })
  for (const event of events) {
    const d = event.data || {}
    if (event.event === 'knowledge.prepared' && d.knowledge_pages > 0)
      put('knowledge', '已查阅业务资料', `找到 ${d.knowledge_pages} 个相关资料页面，用于理解本次需求。`)
    if (event.event === 'rehearsal.ready')
      put('understanding', d.questions ? '已整理待确认口径' : '已明确查询范围', d.questions ? '会影响结果的问题已集中整理，请通过确认卡片补充。' : '已整理本次统计的范围和处理方式。')
    if (event.event === 'requirement.aligned') put('confirmation', '已确认统计口径', '查询将按已确认的范围和条件执行。')
    if (['sql.repaired', 'sql.semantic.corrected', 'query.repair.completed'].includes(event.event))
      put('revision', '已调整查询方案', '已根据检查发现的问题调整查询，具体变化保留在执行记录中。')
    if (event.event === 'execution.completed') put('execution', '已取得查询数据', '已取得本次统计所需的数据。')
    if (event.event === 'answer.completed') put('answer', '已整理查询结果', '查询结果与业务说明已展示。')
  }
  return [...rows.values()]
}

export function resultDescription(run: QueryRun) {
  const answer = (run.answer || '').trim()
  const explanation = answer.replace(/\s*本次共找到\s*[\d,]+\s*条结果。?$/, '').trim()
  const scope = (run.requirement?.summary || run.requirement_draft?.summary || '').trim()
  if (explanation && explanation !== scope) return answer
  if (!run.result) return ''
  if (!run.result.row_count) return '按已确认条件查询，未找到符合条件的记录。'
  return `本次查询返回 ${run.result.row_count.toLocaleString()} 条结果。` + (run.result.truncated ? '结果已达到返回上限，可能还有符合条件的记录。' : '')
}

export function businessFailure(run: QueryRun) {
  const raw = run.error || ''
  if (run.delivery?.kind === 'cancelled') return '本次查询已取消，您的需求和已确认口径仍然保留。'
  const failure = sqlFailure(run)
  if (failure?.error_type === 'timeout') return `查询失败，${failure.message}`
  if (/连接|超时|timeout|network|502|503|1045|authentication|access denied/i.test(raw))
    return '查询所需的服务暂时未能完成响应。您的需求和已确认口径已保留，可以重试本次查询。'
  return '本次查询暂未完成。您的需求和已确认口径已保留，可重试；具体原因可在执行记录中查看。'
}

export function sqlFailure(run: QueryRun): Record<string, any> | undefined {
  if (run.status !== 'failed' || run.delivery?.kind === 'cancelled') return
  const failure = [...run.events].reverse().find(e => e.event === 'source.step.failed')
  if (failure) return failure.data
  // Older tasks have no explicit failed-step event. Only infer an execution
  // failure when a started step has no completion receipt.
  const started = [...run.events].reverse().find(e => e.event === 'source.step.started')
  if (!started || run.events.some(e => e.event === 'source.step.completed' && e.data.step === started.data.step)) return
  const steps = run.plan?.sql_program?.steps || []
  const index = steps.findIndex((s: any) => s.name === started.data.step)
  const step = steps[index]
  const artifact = run.source_artifacts?.[started.data.step]
  if (index < 0 || !artifact) return
  return { step_index: index + 1, purpose: step.purpose, sql: artifact.physical_sql,
    data_source_label: artifact.compilation?.data_source_label, message: run.error }
}
