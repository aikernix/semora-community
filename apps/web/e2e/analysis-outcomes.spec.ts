import { expect, test } from '@playwright/test'
import { analysisOutcomes } from '../src/analysisTimeline'
import type { RunEvent } from '../src/api'

const event = (name: string, data: Record<string, any>): RunEvent => ({ event: name, data, status: 'understanding', created_at: '2026-11-05T13:00:00Z' })
const events = [
  event('agent.request.progress', { summary: '等待 15 秒，推理字符 8000' }),
  event('agent.request.progress', { summary: '等待 30 秒，推理字符 16000' }),
  event('agent.tool.completed', { tool: 'search_wiki', observation: { keyword: '记录', count: 0, pages: [] } }),
  event('agent.tool.completed', { tool: 'read_object', observation: { object: 'record', label: '记录', field_count: 3, relations: [] } }),
  event('agent.tool.completed', { tool: 'read_object', observation: { object: 'record', label: '记录', field_count: 3, relations: [] } }),
  event('plan.candidate.validated', { summary: '按确认阈值筛选记录', revision: true, conclusions: ['阈值已落实到过滤条件'], changes: [{ path: '/filters/0/value', before: 10, after: 20 }] }),
]

test('timeline includes outputs only and collapses duplicate lookups', () => {
  const rows = analysisOutcomes(events)
  expect(rows.map(r => r.title)).toEqual(['取得对象定义', '修订查询方案'])
  expect(rows[1].changes?.[0].before).toBe(10)
  expect(analysisOutcomes(events.slice(0, 3))).toEqual([])
})

test('execution record shows findings and excludes request diagnostics', async ({ page }) => {
  const run = { id: 'outcomes-run', session_id: 'outcomes-session', domain: 'generic', question: '查询记录', status: 'failed', evidence: [], error: '测试结束', events }
  await page.route('**/api/v1/**', route => {
    const path = new URL(route.request().url()).pathname
    if (path === '/api/v1/sessions') return route.fulfill({ json: [{ id: 'outcomes-session', title: '过程展示测试', domain: 'generic', run_count: 1 }] })
    if (path === '/api/v1/domains') return route.fulfill({ json: [{ domain: 'generic', name: '通用示例' }] })
    if (path === '/api/v1/model/status') return route.fulfill({ json: { configured: true } })
    if (path === '/api/v1/settings') return route.fulfill({ json: { approval_mode: 'disabled' } })
    if (path === '/api/v1/sessions/outcomes-session/runs') return route.fulfill({ json: [run] })
    return route.fulfill({ json: {} })
  })
  await page.goto('/workbench')
  await page.getByRole('button', { name: '查看本次查询的执行记录' }).click()
  await expect(page.locator('.record-timeline > li')).toHaveCount(2)
  await expect(page.getByText('等待 15 秒，推理字符 8000', { exact: true })).not.toBeVisible()
  await page.getByText('查看依据与变化', { exact: true }).last().click()
  await expect(page.getByText('修改前：10', { exact: true })).toBeVisible()
  await expect(page.getByText('修改后：20', { exact: true })).toBeVisible()
})
