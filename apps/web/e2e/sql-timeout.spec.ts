import { test, expect } from '@playwright/test'

for (const [origin, message] of [['database', 'SQL执行超时'], ['system', '单条SQL执行超过30秒']]) {
  test(`SQL timeout identifies the failed statement: ${origin}`, async ({ page }) => {
    const steps = [
      { name: 'first', purpose: '读取主体', sql: 'SELECT id FROM subjects', output_columns: ['id'] },
      { name: 'second', purpose: '读取对应信息', sql: 'SELECT id FROM details WHERE id IN (:keys)', output_columns: ['id'] },
    ]
    const sql = 'SELECT id FROM details WHERE id IN (%(p0)s) LIMIT 1001'
    const run = {
      id: 'r', session_id: 's', domain: 'generic', question: '读取主体及对应信息', status: 'failed',
      error: `查询失败，${message}`, evidence: [], result: null,
      plan: { sql_program: { steps } }, source_artifacts: {},
      events: [
        { event: 'source.step.completed', status: 'executing', data: { step: 'first', row_count: 2 } },
        { event: 'source.step.failed', status: 'executing', data: { step: 'second', step_index: 2,
          purpose: '读取对应信息', data_source_label: '第二数据源', sql, error_type: 'timeout', timeout_origin: origin, message } },
        { event: 'run.failed', status: 'failed', data: { error: `查询失败，${message}` } },
      ],
    }
    await page.route('**/api/v1/**', route => {
      const path = new URL(route.request().url()).pathname
      if (path === '/api/v1/sessions') return route.fulfill({ json: [{ id: 's', title: '超时定位验收', domain: 'generic', run_count: 1 }] })
      if (path === '/api/v1/domains') return route.fulfill({ json: [{ domain: 'generic', name: '通用示例' }] })
      if (path === '/api/v1/model/status') return route.fulfill({ json: { configured: true } })
      if (path === '/api/v1/sessions/s/runs') return route.fulfill({ json: [run] })
      return route.fulfill({ json: {} })
    })
    await page.goto('/workbench')
    await expect(page.locator('.failure-section')).toContainText(`查询失败，${message}`)
    await expect(page.locator('.failed-sql')).toContainText('失败 SQL：第 2 条')
    await expect(page.locator('.failed-sql')).toContainText('第二数据源')
    await expect(page.locator('.failed-sql pre')).toHaveText(sql)
    await expect(page.locator('.status-heading')).toHaveText('!查询失败')
  })
}

test('the running page shows the one allowed timeout retry', async ({ page }) => {
  const run = { id: 'r', session_id: 's', domain: 'generic', question: '查询记录', status: 'executing',
    evidence: [], result: null, plan: { sql_program: { steps: [{ name: 'read', purpose: '读取对应信息' }] } },
    events: [{ event: 'source.step.retry', status: 'executing', data: { step: 'read', attempt: 2, max_attempts: 2,
      summary: '本条SQL执行超时，正在重试（第2次，共2次）' } }],
  }
  await page.route('**/api/v1/**', route => {
    const path = new URL(route.request().url()).pathname
    if (path === '/api/v1/sessions') return route.fulfill({ json: [{ id: 's', title: '重试验收', domain: 'generic', run_count: 1 }] })
    if (path === '/api/v1/domains') return route.fulfill({ json: [{ domain: 'generic', name: '通用示例' }] })
    if (path === '/api/v1/model/status') return route.fulfill({ json: { configured: true } })
    if (path === '/api/v1/sessions/s/runs') return route.fulfill({ json: [run] })
    if (path === '/api/v1/runs/r') return route.fulfill({ json: run })
    return route.fulfill({ json: {} })
  })
  await page.goto('/workbench')
  await expect(page.locator('.running-section')).toContainText('正在重试（第2次，共2次）：读取对应信息')
  await expect(page.locator('.failure-section')).toHaveCount(0)
})
