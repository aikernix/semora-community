import { expect, test } from '@playwright/test'

test('query history searches across pages and deletes only the confirmed request', async ({ page }) => {
  let sessions = Array.from({ length: 12 }, (_, index) => ({
    id: `session-${index}`, title: index === 11 ? 'Monthly Revenue' : `查询 ${index}`,
    domain: 'demo', created_at: new Date(Date.UTC(2026, 0, 20 - index)).toISOString(), run_count: 0,
  }))
  const deletes: string[] = []
  await page.route('**/api/v1/**', route => {
    const request = route.request()
    const path = new URL(request.url()).pathname
    if (path === '/api/v1/domains') return route.fulfill({ json: [{ domain: 'empty', name: '无数据源业务域', status: 'unconfigured' }, { domain: 'demo', name: '演示业务域', status: 'configured' }] })
    if (path === '/api/v1/model/status') return route.fulfill({ json: { configured: true } })
    if (path === '/api/v1/sessions') return route.fulfill({ json: sessions })
    if (path.endsWith('/runs')) return route.fulfill({ json: [] })
    if (request.method() === 'DELETE') {
      const id = path.split('/').at(-1)!
      deletes.push(id)
      sessions = sessions.filter(session => session.id !== id)
      return route.fulfill({ json: { deleted: true } })
    }
    return route.fulfill({ status: 404 })
  })

  await page.goto('/workbench')
  await expect(page.locator('.session-item')).toHaveCount(10)
  await page.locator('.domain-picker').click()
  await expect(page.locator('.t-select-option:visible')).toHaveText(['演示业务域'])
  await page.locator('.t-select-option:visible').click()
  await page.getByRole('button', { name: '下一页' }).click()
  await expect(page.locator('.session-item')).toHaveCount(2)
  await expect(page.locator('.session-pagination')).toContainText('2 / 2')

  const search = page.getByPlaceholder('搜索查询名称')
  await search.fill(' REVen ')
  await expect(page.locator('.session-item')).toHaveCount(1)
  await expect(page.locator('.session-item')).toContainText('Monthly Revenue')

  const remove = page.getByRole('button', { name: '删除查询 Monthly Revenue', exact: true })
  await remove.click()
  await page.getByRole('button', { name: '取消', exact: true }).click()
  await expect(page.getByRole('button', { name: '确认删除' })).not.toBeVisible()
  expect(deletes).toEqual([])
  await remove.click()
  await page.getByRole('button', { name: '确认删除' }).click()
  await expect(page.getByText('没有匹配的查询')).toBeVisible()
  expect(deletes).toEqual(['session-11'])
  await search.clear()
  await expect(page.locator('.session-item')).toHaveCount(10)
})
