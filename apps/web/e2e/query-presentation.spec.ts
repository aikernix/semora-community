import { test, expect } from '@playwright/test'
import { businessOutcomes, mergeRunEvent, resultDescription } from '../src/queryPresentation'
import { analysisOutcomes } from '../src/analysisTimeline'
import type { QueryRun, RunEvent } from '../src/api'

const event = (name: string, data: Record<string, unknown> = {}, status: RunEvent['status'] = 'understanding'): RunEvent =>
  ({ event: name, status, data, created_at: '2026-11-08T10:00:00Z' })

// No browser fixtures: these verify stream identity and business projection.
test('reconnect replay and stale snapshots do not duplicate events; gaps require a snapshot', () => {
  const run = { id: 'r', status: 'understanding', events: [] } as unknown as QueryRun
  const item = event('rehearsal.ready', { questions: 1 })
  expect(mergeRunEvent(run, 1, item)).toBe('applied')
  expect(mergeRunEvent(run, 1, item)).toBe('ignored')
  expect(mergeRunEvent(run, 3, event('answer.completed', {}, 'completed'))).toBe('gap')
  expect(run.status).toBe('understanding')
  expect(run.events).toHaveLength(1)
})

test('business milestones update in place while distinct source steps remain in the execution record', () => {
  const rows = [
    event('rehearsal.ready', { questions: 2 }),
    event('alignment.required', { requirement: { clarifications: [{ question: 'Which period?', reason: 'Changes scope' }] } }),
    event('alignment.required', { requirement: { clarifications: [{ question: 'Which period?', reason: 'Changes scope' }] } }),
    event('rehearsal.ready', { questions: 0 }),
    event('requirement.aligned'),
    event('source.step.completed', { step: 'a', summary: 'Get records', row_count: 100 }),
    event('source.step.completed', { step: 'b', summary: 'Get records', row_count: 100 }),
    event('execution.completed', {}, 'completed'),
  ]
  const business = businessOutcomes(rows)
  expect(business.filter(r => r.key === 'understanding')).toHaveLength(1)
  expect(business.find(r => r.key === 'understanding')?.title).toBe('已明确查询范围')
  const details = analysisOutcomes(rows)
  expect(details.filter(r => r.title === '需要你补充')).toHaveLength(1)
  expect(details.filter(r => r.title === '完成数据查询')).toHaveLength(2)
})

test('separate correction attempts remain visible even when their explanations match', () => {
  const rows = [event('sql.repaired', { step: 'a', attempt: 1, summary: 'Correct reference' }),
    event('sql.repaired', { step: 'a', attempt: 2, summary: 'Correct reference' })]
  expect(analysisOutcomes(rows)).toHaveLength(2)
  expect(businessOutcomes(rows)).toHaveLength(1)
})

test('result explanation is not repeated when it only restates the scope and row count', () => {
  const run = { requirement: { summary: '统计全部记录。' }, answer: '统计全部记录。 本次共找到 10 条结果。' } as QueryRun
  expect(resultDescription(run)).toBe('')
  run.answer = '符合条件的记录主要集中在本月。'
  expect(resultDescription(run)).toBe('符合条件的记录主要集中在本月。')
})

test('a timeout retry is visible in execution history without being a SQL rewrite', () => {
  const events = [event('source.step.retry', { step: 'read', attempt: 2, max_attempts: 2,
    summary: '本条SQL执行超时，正在重试（第2次，共2次）' }, 'executing')]
  expect(analysisOutcomes(events)).toMatchObject([{ title: '重试超时查询', summary: '本条SQL执行超时，正在重试（第2次，共2次）' }])
  expect(businessOutcomes(events).some(e => e.key === 'revision')).toBe(false)
})
