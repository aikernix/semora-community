import { test, expect } from '@playwright/test'

test('pending and active relationships edit in a dialog and save as active', async ({ page }) => {
  const records = ['pending', 'accepted'].map((status, i) => ({
    id: `r${i}`, status, source_table_id: 'public.events', target_table_id: 'public.entities',
    source_columns: ['entity_id'], target_columns: ['id'],
    key_pairs: [{ source: 'entity_id', target: 'id' }], cardinality: 'N:1',
  }))
  const writes: unknown[] = []
  await page.route('**/api/v1/**', async route => {
    const url = new URL(route.request().url()), path = url.pathname
    let json: any = []
    if (path.endsWith('/datasources')) json = [{ id: 'fixture', name: '测试结构', type: 'postgresql', status: 'healthy', objects: 2 }]
    else if (path.endsWith('/connection')) json = { type: 'postgresql', host: 'localhost', database: 'fixture' }
    else if (path.endsWith('/onboarding')) json = { id: 'finished', status: 'completed', phase: 'complete', progress: 100 }
    else if (path.endsWith('/tables/detail')) json = { columns: [{ name: url.searchParams.get('table_id') === 'public.events' ? 'entity_id' : 'id' }] }
    else if (path.endsWith('/tables')) json = { items: [{ id: 'public.events' }, { id: 'public.entities' }], total: 2 }
    else if (path.includes('/relationships/r') && route.request().method() === 'PUT') {
      const item = records.find(r => path.endsWith('/' + r.id))!
      Object.assign(item, route.request().postDataJSON(), { status: 'accepted' })
      writes.push(route.request().postDataJSON()); json = item
    } else if (path.endsWith('/relationships')) {
      const status = url.searchParams.get('status')
      json = { items: records.filter(r => !status || status === 'all' || r.status === status), total: records.length,
        counts: { all: 2, accepted: records.filter(r => r.status === 'accepted').length,
          pending: records.filter(r => r.status === 'pending').length, disabled: 0 } }
    }
    await route.fulfill({ json })
  })
  await page.goto('/datasources')
  await page.getByRole('button', { name: /查看关联关系/ }).click()
  for (const index of [0, 1]) {
    await page.locator('.relation-card').nth(index).getByRole('button', { name: '编辑', exact: true }).click()
    const dialog = page.locator('.t-dialog:visible').filter({ hasText: '编辑关联关系' })
    await expect(dialog).toBeVisible()
    await expect(dialog.getByText('关联条件', { exact: true })).toBeVisible()
    await dialog.getByRole('button', { name: '保存并生效', exact: true }).click()
    await expect(dialog).not.toBeVisible()
    await expect(page.locator('.relation-card').nth(index)).toContainText('已生效')
  }
  expect(writes).toHaveLength(2)
})

test('source analysis requires confirmation and offers cancellation while running',async({page})=>{
  let status='completed',starts=0,cancels=0
  const job=()=>({id:'source-analysis',status,phase:status==='completed'?'complete':status,progress:status==='completed'?100:45})
  await page.route('**/api/v1/**',route=>{
    const path=new URL(route.request().url()).pathname
    if(path.endsWith('/datasources'))return route.fulfill({json:[{id:'fixture',name:'测试结构',type:'postgresql',status:'healthy',objects:2}]})
    if(path.endsWith('/connection'))return route.fulfill({json:{type:'postgresql',host:'localhost'}})
    if(path.endsWith('/onboarding'))return route.fulfill({json:job()})
    if(path.endsWith('/relationships/analyze')){starts++;status='profiling';return route.fulfill({json:job()})}
    if(path.endsWith('/cancel')){cancels++;status='cancelled';return route.fulfill({json:job()})}
    return route.fulfill({json:[]})
  })
  await page.goto('/datasources')
  await page.getByRole('button',{name:'重新分析关联关系',exact:true}).click()
  expect(starts).toBe(0)
  await page.locator('.t-dialog:visible').getByRole('button',{name:'取消',exact:true}).click()
  expect(starts).toBe(0)
  await page.getByRole('button',{name:'重新分析关联关系',exact:true}).click()
  await page.locator('.t-dialog:visible').getByRole('button',{name:'确认分析',exact:true}).click()
  await expect(page.getByRole('button',{name:'取消任务',exact:true})).toBeEnabled()
  await page.getByRole('button',{name:'取消任务',exact:true}).click()
  await expect(page.locator('.task-panel').getByText('停止于 45%',{exact:true})).toBeVisible()
  await expect(page.locator('.task-panel').getByText('已有生效关联保持可用。',{exact:true})).toBeVisible()
  expect(starts).toBe(1);expect(cancels).toBe(1)
})
