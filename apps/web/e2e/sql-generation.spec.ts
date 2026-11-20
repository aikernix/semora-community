import { test, expect } from '@playwright/test'

test('historical SQL-only records remain readable without legacy controls', async ({ page }) => {
  const run = {
    id: 'sql-run', session_id: 'sql-session', domain: 'generic', question: '列出当前记录',
    status: 'completed', evidence: [], events: [], source_artifacts: {},
    sql: { physical_sql: 'SELECT id FROM items LIMIT 1000', dialect: 'postgres',
      artifact_hash: 'test-artifact', parameters: {}, compilation: { validation: { state: 'validated' } } },
    sql_explanation: '读取记录标识，最多返回 1000 行。',
    delivery: { kind: 'sql', title: 'SQL 已生成', summary: '已生成并校验 SQL，未执行业务查询。',
      warnings: [], next_action: '可以继续补充口径。', result_verified: false },
  }
  await page.route('**/api/v1/**', route => {
    const path = new URL(route.request().url()).pathname
    if (path === '/api/v1/sessions') return route.fulfill({ json: [{ id: 'sql-session', title: 'SQL 生成测试', domain: 'generic', run_count: 1 }] })
    if (path === '/api/v1/domains') return route.fulfill({ json: [{ domain: 'generic', name: '通用示例' }] })
    if (path === '/api/v1/model/status') return route.fulfill({ json: { configured: true } })
    if (path === '/api/v1/sessions/sql-session/runs') return route.fulfill({ json: [run] })
    return route.fulfill({ json: {} })
  })
  await page.goto('/workbench')
  await expect(page.getByText('查询语句已生成', { exact: true })).toBeVisible()
  await expect(page.locator('.code-block')).toHaveCount(0)
  await page.getByRole('button', { name: '查看本次查询的执行记录' }).click()
  await page.getByRole('button', { name: '查询语句', exact: true }).click()
  await expect(page.locator('.code-block').first()).toContainText('SELECT id FROM items')
  await expect(page.getByText('数据库校验通过', { exact: false })).toBeVisible()
  await expect(page.getByText('审批中心', { exact: true })).toHaveCount(0)
  await expect(page.getByText('审计回放', { exact: true })).toHaveCount(0)
  await expect(page.getByText('门禁与 Reviewer', { exact: false })).toHaveCount(0)
  await expect(page.locator('.result-table')).toHaveCount(0)
  await expect(page.locator('.composer')).toHaveCount(0)
  await expect(page.getByText('查看分析自评', { exact: false })).toHaveCount(0)
  await expect(page.getByText('技术诊断', { exact: false })).toHaveCount(0)
})
