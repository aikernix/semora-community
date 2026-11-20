import {test,expect} from '@playwright/test'

test('incomplete relationship analysis retains results and offers checkpoint resume',async({page})=>{
  let job:any={id:'partial',status:'completed_with_pending',phase:'incomplete',progress:100,
    details:{technical_incomplete:true,checkpoint_ref:'saved',incomplete_count:1}}
  let resumes=0
  await page.route('**/api/v1/**',route=>{
    const path=new URL(route.request().url()).pathname
    if(path==='/api/v1/datasources')return route.fulfill({json:[{id:'source',name:'测试数据源',type:'postgresql',status:'healthy',objects:2}]})
    if(path.endsWith('/onboarding/partial/resume')){
      resumes++;job={...job,status:'profiling',phase:'comparing',progress:100}
      return route.fulfill({json:job})
    }
    if(path.endsWith('/onboarding'))return route.fulfill({json:job})
    if(path.endsWith('/connection'))return route.fulfill({json:{host:'localhost',database:'fixture',username:'reader'}})
    return route.fulfill({json:[]})
  })
  await page.goto('/datasources')
  await expect(page.locator('.task-panel')).toContainText('部分未完成')
  await expect(page.locator('.task-panel')).toContainText('原生效关系保留')
  await page.getByRole('button',{name:'继续上次分析',exact:true}).click()
  await expect(page.locator('.task-panel')).toContainText('进行中')
  await expect(page.getByRole('button',{name:'继续上次分析',exact:true})).toBeDisabled()
  expect(resumes).toBe(1)
})

test('source details fill the page and creation stays in an isolated dialog',async({page})=>{
  await page.setViewportSize({width:1920,height:1080})
  const sources:any[]=[{id:'existing',name:'已有数据源',type:'postgresql',status:'healthy',objects:3}]
  let created:any,tests=0
  await page.route('**/api/v1/**',route=>{
    const path=new URL(route.request().url()).pathname
    if(path==='/api/v1/datasource-products')return route.fulfill({json:[{id:'postgresql',label:'PostgreSQL',port:5432}]})
    if(path==='/api/v1/datasources'&&route.request().method()==='POST'){
      created=route.request().postDataJSON();sources.push({id:'new',name:created.name,type:'postgresql',status:'healthy',objects:0});return route.fulfill({json:{id:'new'}})
    }
    if(path==='/api/v1/datasources')return route.fulfill({json:sources})
    if(path.endsWith('/connection'))return route.fulfill({json:{host:'original-host',database:'original-db',username:'reader',port:5432}})
    if(path.endsWith('/onboarding'))return route.fulfill({json:{id:'done',status:'completed',phase:'metadata_complete',progress:100}})
    if(path==='/api/v1/datasource-connection-test'){tests++;return route.fulfill({json:{test_receipt:'valid-receipt',database:'fixture',version:'16'}})}
    return route.fulfill({json:[]})
  })
  await page.goto('/datasources')
  await expect(page.locator('.source-form-panel h2')).toHaveText('已有数据源')
  await expect(page.locator('.connection-panel')).toContainText('original-host')
  await expect(page.locator('.task-panel')).toContainText('任务执行情况')
  const box=await page.locator('.workspace').boundingBox()
  expect(box!.x+box!.width).toBeGreaterThan(1840)
  await page.getByRole('button',{name:'添加数据源',exact:true}).click()
  let dialog=page.locator('.t-dialog:visible')
  await expect(dialog.getByRole('button',{name:'创建数据源',exact:true})).toBeDisabled()
  await dialog.getByRole('textbox',{name:'数据源名称',exact:true}).fill('取消的草稿')
  await expect(page.locator('.source-form-panel h2')).toHaveText('已有数据源')
  await dialog.getByRole('button',{name:'取消',exact:true}).click()
  await page.getByRole('button',{name:'添加数据源',exact:true}).click()
  dialog=page.locator('.t-dialog:visible')
  await expect(dialog.getByRole('textbox',{name:'数据源名称',exact:true})).toHaveValue('')
  await dialog.getByRole('textbox',{name:'数据源名称',exact:true}).fill('新数据源')
  await dialog.getByRole('textbox',{name:'数据库地址',exact:true}).fill('new-host')
  await dialog.getByRole('button',{name:'测试连接',exact:true}).click()
  await expect(dialog.getByRole('button',{name:'创建数据源',exact:true})).toBeEnabled()
  await dialog.getByRole('textbox',{name:'数据库地址',exact:true}).fill('changed-host')
  await expect(dialog.getByRole('button',{name:'创建数据源',exact:true})).toBeDisabled()
  await dialog.getByRole('button',{name:'测试连接',exact:true}).click()
  await expect(dialog.getByRole('button',{name:'创建数据源',exact:true})).toBeEnabled()
  await dialog.getByRole('button',{name:'创建数据源',exact:true}).click()
  await expect(page.locator('.source-form-panel h2')).toHaveText('新数据源')
  expect(tests).toBe(2)
  expect(created.test_receipt).toBe('valid-receipt')
  expect(created.connection.host).toBe('changed-host')
})

test('polling checks active jobs only and stops after completion',async({page})=>{
  const calls:Record<string,number>={active:0,idle:0,list:0}
  let busy=0,peak=0
  await page.route('**/api/v1/**',async route=>{
    const path=new URL(route.request().url()).pathname
    if(path==='/api/v1/datasources'){
      calls.list++;return route.fulfill({json:[{id:'active',name:'运行库',type:'postgresql',status:'healthy',objects:2},
        {id:'idle',name:'空闲库',type:'postgresql',status:'healthy',objects:1}]})
    }
    if(path.endsWith('/onboarding')){
      const id=path.includes('/active/')?'active':'idle';calls[id]++
      if(id==='idle')return route.fulfill({status:404,json:{detail:'no task'}})
      busy++;peak=Math.max(peak,busy)
      if(calls.active>1)await new Promise(resolve=>setTimeout(resolve,4000))
      busy--
      return route.fulfill({json:{id:'job',status:calls.active===1?'profiling':'completed',phase:calls.active===1?'comparing':'complete',progress:calls.active===1?54:100}})
    }
    if(path.endsWith('/connection'))return route.fulfill({json:{host:'localhost',database:'fixture',username:'reader'}})
    return route.fulfill({json:[]})
  })
  await page.goto('/datasources')
  await expect(page.locator('.task-panel')).toContainText('进行中')
  await expect(page.locator('.task-panel')).toContainText('关联关系已更新',{timeout:15000})
  expect(calls.active).toBe(2);expect(calls.idle).toBe(1);expect(peak).toBe(1)
  const completed={...calls}
  await page.waitForTimeout(3500)
  expect(calls).toEqual(completed)
})

test('table scans show terminal outcomes and preserve the relationship task', async ({ page }) => {
  const relationship = { id: 'analysis', status: 'cancelled', phase: 'cancelled', progress: 44 }
  let current: Record<string, any> = relationship
  await page.route('**/api/v1/**', route => {
    const url = new URL(route.request().url())
    if (url.pathname.endsWith('/datasources')) {
      return route.fulfill({ json: [{ id: 'source', name: '演示数据源', type: 'postgresql', status: 'healthy', objects: 2 }] })
    }
    if (url.pathname.endsWith('/rescan')) {
      current = { id: 'scan', status: 'scanning', phase: 'scanning', progress: 32, details: { metadata_only: true } }
      return route.fulfill({ json: current })
    }
    if (url.pathname.endsWith('/onboarding')) {
      return route.fulfill({ json: url.searchParams.has('relationships_only') ? relationship : current })
    }
    return route.fulfill({ json: url.pathname.endsWith('/connection') ? {} : [] })
  })

  await page.goto('/datasources')
  const panel = page.locator('.task-panel')
  const rescan = panel.getByRole('button', { name: '重新扫描表信息', exact: true })
  await Promise.all([page.waitForResponse(response => response.url().endsWith('/rescan')), rescan.click()])
  await expect(rescan).toBeDisabled()
  await expect(panel).toContainText('停止于 44%')
  await expect(panel).not.toContainText('32%')
  await expect(panel.locator('.scan-outcome')).toHaveCount(0)
  current = { ...current, status: 'completed', phase: 'metadata_complete', progress: 100 }
  await expect(panel.locator('.scan-outcome')).toHaveText('扫描表信息成功')
  await page.reload()
  await expect(panel).toContainText('停止于 44%')
  await Promise.all([page.waitForResponse(response => response.url().endsWith('/rescan')), rescan.click()])
  current = { ...current, status: 'failed', phase: 'failed', error: '连接不可用' }
  await expect(panel.locator('.scan-outcome')).toHaveText('扫描表信息失败：连接不可用')
  await expect(panel).toContainText('停止于 44%')
})
