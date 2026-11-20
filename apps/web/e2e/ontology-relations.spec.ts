import { test, expect, type Page } from '@playwright/test'

async function studio(page: Page, openDrawer=true) {
  const configuration = {
    domain: 'fixture', name: '关联演示', version: '1.0.0',
    objects: [
      { name: 'customer', label: '客户', table: 'demo.customer', dataSourceId: 'db', primaryKey: 'id', properties: [{ name: 'id', column: 'id', type: 'string' }, {name:'tenant_id',column:'tenant_id',type:'string'}] },
      { name: 'loan', label: '贷款', table: 'demo.loan', dataSourceId: 'db2', primaryKey: 'id', properties: [{ name: 'customer_id', column: 'customer_id', type: 'string' }, {name:'tenant_id',column:'tenant_id',type:'string'}] },
    ],
    metrics: [{ name: 'loan_count', expression: 'COUNT(loan.id)', grain: 'customer' }],
    relations: [{ name: 'customer_loan', label: '客户持有贷款', from: 'customer', to: 'loan', cardinality: '1:N', joinType: 'one_to_many', condition: 'customer.id = loan.customer_id', metadata: { origin: 'manual', compound: true } }],
  }
  let current: any = structuredClone(configuration)
  const writes: any[] = []
  let rejectNext = false
  await page.route('**/api/v1/ontology/configurations*', async route => {
    if (route.request().method() === 'PUT') {
      const body = route.request().postDataJSON()
      writes.push(body)
      if (rejectNext) { rejectNext = false; await route.fulfill({ status: 422, json: { detail: '关联条件引用了不存在的对象' } }); return }
      current = structuredClone(body)
      await route.fulfill({ json: current })
    } else await route.fulfill({ json: [{ domain: 'fixture', name: '关联演示' }] })
  })
  await page.route('**/api/v1/ontology/configurations/fixture', async route => {
    if (route.request().method() === 'PUT') {
      const body = route.request().postDataJSON()
      writes.push(body)
      if (rejectNext) { rejectNext = false; await route.fulfill({ status: 422, json: { detail: '关联条件引用了不存在的对象' } }); return }
      current = structuredClone(body)
    }
    await route.fulfill({ json: current })
  })
  await page.route('**/api/v1/datasources', route => route.fulfill({ json: [{ id: 'db', name: '演示库', type: 'mysql' }, { id: 'db2', name: '第二库', type: 'mysql' }, { id: 'extra', name: '可添加库', type: 'postgresql' }, { id: 'unmodeled', name: '未建模库', type: 'postgresql' }] }))
  await page.route('**/api/v1/datasources/*/source-ontology', route => new URL(route.request().url()).pathname.endsWith('/unmodeled/source-ontology')
    ? route.fulfill({ status: 404, json: { detail: '数据源尚未生成单源本体包' } })
    : route.fulfill({ json: { status: 'ready', object_count: 1, relation_count: 0 } }))
  await page.route('**/api/v1/ontology/configurations/fixture/cross-source-analysis',route=>route.fulfill({json:{items:[]}}))
  await page.route('**/api/v1/ontology/configurations/fixture/sources/extra', async route => {
    current = { ...current, sourcePackageRefs: [{ dataSourceId: 'db' }, { dataSourceId: 'db2' }, { dataSourceId: 'extra' }] }
    await route.fulfill({ json: current })
  })
  const inactive = new Map<string, any>()
  await page.route(/\/api\/v1\/ontology\/configurations\/fixture\/relationships(?:\/.*)?$/, async route => {
    const request=route.request(), parts=new URL(request.url()).pathname.split('/'), identifier=parts[parts.length-1]==='decision'?parts.at(-2)!:parts.at(-1)!
    if(request.method()==='GET'){
      const items=[...current.relations.filter((r:any)=>r.from!==r.to).map((relation:any)=>({id:relation.name,status:'accepted',relation})),...inactive.values()]
      await route.fulfill({json:{items}});return
    }
    if(rejectNext){rejectNext=false;await route.fulfill({status:422,json:{detail:'关联条件引用了不存在的对象'}});return}
    if(request.method()==='PUT'){
      const relation=request.postDataJSON()
      current.relations=current.relations.filter((r:any)=>r.name!==identifier).concat([relation]);inactive.delete(identifier)
    }else{
      const action=request.postDataJSON().action
      const relation=current.relations.find((r:any)=>r.name===identifier)||inactive.get(identifier)?.relation
      current.relations=current.relations.filter((r:any)=>r.name!==identifier)
      if(['approve','enable'].includes(action)){current.relations.push(relation);inactive.delete(identifier)}
      else inactive.set(identifier,{id:identifier,status:'disabled',relation})
    }
    writes.push(structuredClone(current));await route.fulfill({json:current})
  })
  await page.route('**/api/v1/ontology/configurations/fixture/sources/db2',async route=>{
    current.objects=current.objects.filter((o:any)=>o.dataSourceId!=='db2');current.relations=[]
    current.sourcePackageRefs=[{dataSourceId:'db'}];inactive.clear()
    await route.fulfill({json:current})
  })
  await page.goto('/ontology')
  await expect(page.getByRole('heading', { name: '本体工作室' })).toBeVisible()
  await expect(page.locator('.source-pair-card')).toHaveCount(1)
  if(openDrawer){await page.getByRole('button',{name:'查看关联关系',exact:true}).click();await expect(page.locator('.relation-card')).toHaveCount(1)}
  return { configuration, writes, reject: () => { rejectNext = true },
    seedPending:()=>inactive.set('uncertain',{id:'uncertain',status:'pending',relation:{...structuredClone(configuration.relations[0]),name:'uncertain',label:'待核查关系',cardinality:'unknown'}}) }
}

test('studio lists bound sources only and adds sources through the plus dialog', async ({ page }) => {
  await studio(page,false)
  const packages = page.locator('.source-package-panel')
  await expect(packages.locator('.package-row')).toHaveCount(2)
  await expect(packages.locator('.package-row').first()).toContainText('演示库')
  await expect(packages.locator('.package-row').filter({ hasText: '可添加库' })).toHaveCount(0)
  await expect(page.locator('.page-header').getByRole('button', { name: '新增关联', exact: true })).toHaveCount(0)
  await expect(page.locator('.model-panel .section-heading').getByRole('button', { name: '新增关联', exact: true })).toBeVisible()
  await packages.getByRole('button', { name: '＋ 添加数据源' }).click()
  const dialog = page.locator('.t-dialog:visible').filter({ hasText: '添加到当前业务域' })
  await dialog.locator('.t-select').click()
  await expect(page.getByText('未建模库 · postgresql', { exact: true })).toHaveCount(0)
  await page.getByText('可添加库 · postgresql', { exact: true }).click()
  await dialog.getByRole('button', { name: '添加到当前业务域', exact: true }).click()
  await expect(dialog).not.toBeVisible()
  await expect(packages.locator('.package-row')).toHaveCount(3)
  const cards = page.locator('.source-pair-card')
  await expect(cards).toHaveCount(1)
  await expect(cards.filter({ hasText: '可添加库' })).toHaveCount(0)

  let jobs:any[] = []
  await page.route('**/api/v1/ontology/configurations/fixture/cross-source-analysis', async route => {
    if (route.request().method() === 'POST') {
      const body = route.request().postDataJSON()
      expect(body).toEqual({ left_source_id: 'db', right_source_id: 'extra' })
      const job = { id: 'new-pair', datasource_id: 'db', peer_datasource_id: 'extra', status: 'completed' }
      jobs = [job]
      await route.fulfill({ json: job })
    } else await route.fulfill({ json: { items: jobs } })
  })
  const add = page.getByRole('button', { name: '新增关联', exact: true })
  await add.click()
  const picker = page.locator('.t-dialog:visible').filter({ hasText: '新增库间关联' })
  await picker.locator('label').filter({ hasText: '数据源 A' }).locator('.t-select').click()
  await page.locator('.t-popup:visible').getByText('演示库', { exact: true }).click()
  const right = picker.locator('label').filter({ hasText: '数据源 B' }).locator('.t-select')
  await right.click()
  await expect(page.locator('.t-popup:visible').getByText('第二库', { exact: true })).toHaveCount(0)
  await page.locator('.t-popup:visible').getByText('可添加库', { exact: true }).click()
  await picker.getByRole('button', { name: '开始分析', exact: true }).click()
  await expect(cards).toHaveCount(2)
  await expect(cards.filter({ hasText: '可添加库' })).toHaveCount(1)
  await add.click()
  await picker.locator('label').filter({ hasText: '数据源 A' }).locator('.t-select').click()
  await expect(page.locator('.t-popup:visible').getByText('演示库', { exact: true })).toHaveCount(0)

})

test('studio shows only table relationships and edits persist without changing mappings or metrics', async ({ page }) => {
  const { configuration, writes, reject } = await studio(page)
  await expect(page.getByText('对象与字段映射', { exact: true })).toHaveCount(0)
  await expect(page.getByText('指标目录', { exact: true })).toHaveCount(0)
  await expect(page.getByRole('button', { name: '编辑本体', exact: true })).toHaveCount(0)
  await expect(page.locator('.relation-expression').first()).toContainText('demo.customer')
  await page.getByRole('button', { name: '编辑', exact: true }).click()
  const dialog = page.locator('.t-dialog:visible').filter({ hasText: '编辑关联关系' })
  await expect(dialog.getByText('关系名称',{exact:true})).toHaveCount(0)
  await expect(dialog.getByText('关系标识',{exact:true})).toHaveCount(0)
  await dialog.getByRole('button',{name:'附加条件（可选）',exact:true}).click()
  await dialog.locator('textarea').fill('demo.loan.customer_id IS NOT NULL')
  reject()
  await dialog.getByRole('button', { name: '保存并生效' }).click()
  await expect(dialog.getByText('关联条件引用了不存在的对象', { exact: true })).toBeVisible()
  await expect(page.locator('.relation-card')).toContainText('demo.customer')
  await dialog.getByRole('button', { name: '保存并生效' }).click()
  await expect(dialog).toHaveCount(0)
  expect(writes.at(-1).objects).toEqual(configuration.objects)
  expect(writes.at(-1).metrics).toEqual(configuration.metrics)
  expect(writes.at(-1).relations[0].metadata).toEqual(configuration.relations[0].metadata)
  await page.reload()
  await page.getByRole('button',{name:'查看关联关系',exact:true}).click()
  await expect(page.locator('.relation-card')).toContainText('demo.customer')
  await expect(page.locator('.relation-condition')).toContainText('AND (demo.loan.customer_id IS NOT NULL)')
  await page.screenshot({ path: '../../artifacts/ontology-relations/studio.png', fullPage: true })
})

test('relations can be added, cancelled, disabled and enabled', async ({ page }) => {
  const { writes } = await studio(page)
  await page.getByRole('button', { name: '编辑', exact: true }).click()
  let dialog = page.locator('.t-dialog:visible').filter({ hasText: '编辑关联关系' })
  await dialog.getByRole('button',{name:'添加一对字段',exact:true}).click()
  await dialog.getByRole('button', { name: '取消', exact: true }).click()
  expect(writes).toHaveLength(0)
  await page.getByRole('button', { name: '添加关联关系', exact: true }).click()
  dialog = page.locator('.t-dialog:visible').filter({ hasText: '添加关联关系' })
  await dialog.getByRole('button', { name: '保存并生效' }).click()
  await expect(dialog.getByText('请为每一行选择完整的来源字段和目标字段。',{exact:true})).toBeVisible()
  await expect(dialog.getByText('关系名称',{exact:true})).toHaveCount(0)
  await expect(dialog.getByText('关系标识',{exact:true})).toHaveCount(0)
  await dialog.locator('label').filter({ hasText: '来源表' }).locator('.t-select').click()
  await page.getByText('客户 · demo.customer · 演示库', { exact: true }).filter({visible:true}).last().click()
  await expect(dialog.getByRole('textbox',{name:'来源表',exact:true})).toHaveValue('客户 · demo.customer · 演示库')
  await expect(page.locator('.t-select-option:visible')).toHaveCount(0)
  await dialog.locator('label').filter({ hasText: '目标表' }).locator('.t-select').click()
  await page.getByText('贷款 · demo.loan · 第二库', { exact: true }).filter({visible:true}).last().click()
  await expect(dialog.getByRole('textbox',{name:'目标表',exact:true})).toHaveValue('贷款 · demo.loan · 第二库')
  await dialog.locator('.field-mapping-row').locator('label').first().locator('.t-select').click()
  await page.locator('.t-select-option:visible').filter({hasText:/^id$/}).click()
  await expect(page.locator('.t-select-option:visible')).toHaveCount(0)
  await dialog.locator('.field-mapping-row').locator('label').last().locator('.t-select').click()
  await page.locator('.t-select-option:visible').filter({hasText:/^customer_id$/}).click()
  await expect(page.locator('.t-select-option:visible')).toHaveCount(0)
  await dialog.getByRole('button',{name:'添加一对字段',exact:true}).click()
  const second=dialog.locator('.field-mapping-row').nth(1)
  await second.locator('label').first().locator('.t-select').click()
  await page.locator('.t-select-option:visible').filter({hasText:/^tenant_id$/}).click()
  await expect(page.locator('.t-select-option:visible')).toHaveCount(0)
  await second.locator('label').last().locator('.t-select').click()
  await page.locator('.t-select-option:visible').filter({hasText:/^tenant_id$/}).click()
  await expect(dialog.locator('.generated-condition')).toContainText('demo.customer.tenant_id = demo.loan.tenant_id')
  await dialog.getByRole('button', { name: '保存并生效' }).click()
  await expect(page.locator('.relation-card')).toHaveCount(2)
  expect(writes.at(-1).relations.at(-1).condition).toBe('customer.id = loan.customer_id AND customer.tenant_id = loan.tenant_id')
  expect(writes.at(-1).relations.at(-1).name).toMatch(/^r_[a-f0-9]{32}$/)
  await page.locator('.relation-card').filter({ hasText: 'tenant_id' }).getByRole('button', { name: '停用', exact: true }).click()
  const confirmation = page.locator('.t-dialog:visible').filter({ hasText: '停用关联关系' })
  await confirmation.getByRole('button', { name: '取消', exact: true }).click()
  await expect(page.locator('.relation-card')).toHaveCount(2)
  await page.locator('.relation-card').filter({ hasText: 'tenant_id' }).getByRole('button', { name: '停用', exact: true }).click()
  await confirmation.getByRole('button', { name: '确认停用', exact: true }).click()
  await page.locator('.t-tabs').getByText('已生效 1',{exact:true}).click()
  await expect(page.locator('.relation-card')).toHaveCount(1)
  expect(writes.at(-1).relations.map((r: any) => r.name)).toEqual(['customer_loan'])
  await page.locator('.t-tabs').getByText('已停用 1',{exact:true}).click()
  await expect(page.locator('.relation-card')).toContainText('tenant_id')
  await page.getByRole('button',{name:'启用',exact:true}).click()
  await expect(page.locator('.t-tabs').getByText('已生效 2',{exact:true})).toBeVisible()
})

test('bound sources can be detached with confirmation',async({page})=>{
  await studio(page,false)
  await page.locator('.package-row').filter({hasText:'第二库'}).getByRole('button',{name:'解除绑定',exact:true}).click()
  const dialog=page.locator('.t-dialog:visible').filter({hasText:'解除数据源绑定'})
  await dialog.getByRole('button',{name:'解除绑定',exact:true}).click()
  await expect(page.locator('.package-row')).toHaveCount(1)
  await expect(page.locator('.relation-card')).toHaveCount(0)
})

test('pending unknown relations require editing before activation',async({page})=>{
  const {seedPending}=await studio(page)
  seedPending();await page.reload()
  await page.getByRole('button',{name:'查看关联关系',exact:true}).click()
  await page.locator('.t-tabs').getByText('待审核 1',{exact:true}).click()
  const card=page.locator('.relation-card')
  await expect(card.getByRole('button',{name:'同意',exact:true})).toBeDisabled()
  await expect(card.getByRole('button',{name:'拒绝',exact:true})).toBeEnabled()
  await card.getByRole('button',{name:'编辑',exact:true}).click()
  const dialog=page.locator('.t-dialog:visible').filter({hasText:'编辑关联关系'})
  await dialog.locator('label').filter({hasText:'关联基数'}).locator('.t-select').click()
  await page.getByText('一对多 (1:N)',{exact:true}).last().click()
  await dialog.getByRole('button',{name:'保存并生效',exact:true}).click()
  await expect(page.locator('.t-tabs').getByText('待审核 0',{exact:true})).toBeVisible()
  await page.locator('.t-tabs').getByText('已生效 2',{exact:true}).click()
  await expect(page.locator('.relation-card')).toHaveCount(2)
})

test('pair reanalysis stays scoped and updates its open drawer',async({page})=>{
  await studio(page,false)
  let complete=false,body:any
  const job={id:'pair-analysis',datasource_id:'db',peer_datasource_id:'db2',domain:'fixture',status:'profiling',phase:'comparing_cross_source',progress:65}
  await page.route('**/api/v1/ontology/configurations/fixture/cross-source-analysis',async route=>{
    if(route.request().method()==='POST'){body=route.request().postDataJSON();await route.fulfill({json:job})}
    else await route.fulfill({json:{items:[{...job,status:'completed',progress:100}]}})
  })
  await page.route('**/api/v1/ontology/configurations/fixture/cross-source-analysis/pair-analysis',route=>route.fulfill({json:{...job,status:complete?'completed':'profiling',progress:complete?100:65}}))
  await page.getByRole('button',{name:'查看关联关系',exact:true}).click()
  await page.locator('.t-drawer--open').getByRole('button',{name:'更新关联关系',exact:true}).click()
  expect(body).toBeUndefined()
  await page.locator('.t-dialog:visible').getByRole('button',{name:'取消',exact:true}).click()
  expect(body).toBeUndefined()
  await page.locator('.t-drawer--open').getByRole('button',{name:'更新关联关系',exact:true}).click()
  await page.locator('.t-dialog:visible').getByRole('button',{name:'确认分析',exact:true}).click()
  await expect.poll(()=>body).toEqual({left_source_id:'db',right_source_id:'db2',mode:'reanalyze'})
  await expect(page.locator('.source-pair-card')).toContainText('正在分析')
  await expect(page.locator('.t-drawer:visible').getByRole('button',{name:'取消任务',exact:true})).toBeEnabled()
  complete=true
  await expect(page.locator('.source-pair-card')).not.toContainText('正在分析')
  await expect(page.locator('.t-drawer:visible .relation-card')).toHaveCount(1)
  await expect(page.locator('.t-drawer:visible').getByRole('button',{name:'更新关联关系',exact:true})).toBeEnabled()
  await page.reload()
  await expect(page.locator('.source-pair-card')).toContainText('已有关联')
})

test('drawer reanalysis confirms first and can cancel without removing effective relations',async({page})=>{
  await studio(page)
  let status='profiling',starts=0,cancels=0
  const job=()=>({id:'cancel-pair',datasource_id:'db',peer_datasource_id:'db2',domain:'fixture',status,phase:status,progress:65})
  await page.route('**/api/v1/ontology/configurations/fixture/cross-source-analysis',route=>{
    if(route.request().method()==='POST'){starts++;return route.fulfill({json:job()})}
    return route.fulfill({json:{items:[]}})
  })
  await page.route(/\/cross-source-analysis\/cancel-pair(?:\/cancel)?$/,route=>{
    if(route.request().method()==='POST'){cancels++;status='cancelled'}
    return route.fulfill({json:job()})
  })
  const drawer=page.locator('.t-drawer:visible')
  await drawer.getByRole('button',{name:'更新关联关系',exact:true}).click()
  await expect(page.locator('.t-dialog:visible')).toContainText('原有已生效关系保持可用')
  expect(starts).toBe(0)
  await page.locator('.t-dialog:visible').getByRole('button',{name:'确认分析',exact:true}).click()
  await expect(drawer.getByRole('button',{name:'取消任务',exact:true})).toBeEnabled()
  await drawer.getByRole('button',{name:'取消任务',exact:true}).click()
  await expect(drawer.getByRole('button',{name:'更新关联关系',exact:true})).toBeEnabled()
  expect(starts).toBe(1);expect(cancels).toBe(1)
  await expect(drawer.locator('.relation-card')).toHaveCount(1)
  await drawer.getByRole('button',{name:'更新关联关系',exact:true}).click()
  await expect(page.locator('.t-dialog:visible')).toBeVisible()
  expect(starts).toBe(1)
})
