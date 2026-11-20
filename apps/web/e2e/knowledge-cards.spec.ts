import {test,expect,type Page,type Locator} from '@playwright/test'

async function setup(page:Page){
  const bindings:Record<string,string[]>={one:['kb-a','kb-b'],two:['kb-a']}
  const writes:Array<{domain:string;ids:string[]}>=[],research:any[]=[]
  let inaccessible=false
  await page.route('**/api/v1/**',async route=>{
    const path=new URL(route.request().url()).pathname
    if(path==='/api/v1/knowledge/status')return route.fulfill({json:{status:'ready',api_key_configured:true,instance_url:'https://knowledge.invalid'}})
    if(path==='/api/v1/knowledge-bases')return inaccessible?route.fulfill({status:503,json:{detail:'新实例暂不可访问'}}):route.fulfill({json:['a','b','c','d'].map((v,index)=>({id:'kb-'+v,name:['知识库甲','知识库乙','知识库丙','知识库丁'][index],documents:2,description:'用于业务解释的资料'}))})
    if(path==='/api/v1/settings/knowledge'){
      if(route.request().method()==='PUT')inaccessible=true
      return route.fulfill({json:{base_url:'https://knowledge.invalid/api/v1',api_key_configured:true}})
    }
    if(path==='/api/v1/domains')return route.fulfill({json:[{domain:'one',name:'业务域一'},{domain:'two',name:'业务域二'}]})
    const match=path.match(/\/ontology\/drafts\/(one|two)(\/knowledge-bases)?$/)
    if(match){
      const domain=match[1]!
      if(route.request().method()==='PATCH'){
        // Keep the first save in flight so another card can enqueue a save.
        await new Promise(resolve=>setTimeout(resolve,120))
        bindings[domain]=route.request().postDataJSON().knowledgeBaseIds
        writes.push({domain,ids:[...bindings[domain]!]})
      }
      return route.fulfill({json:{domain,knowledgeBaseIds:bindings[domain]}})
    }
    if(path==='/api/v1/knowledge/research'){
      research.push(route.request().postDataJSON())
      return route.fulfill({json:{answer:'找到一个相关知识点',steps:[{kind:'search',summary:'已检索相关资料'}],evidence:[{chunk_id:'chunk',knowledge_base_id:'kb-a',title:'编号规则',summary:'按记录编号识别对应实体。',locator:'规则文档',revision:'v1'}]}})
    }
    return route.fulfill({json:{}})
  })
  await page.goto('/knowledge')
  await expect(page.locator('.binding-card')).toHaveCount(3)
  return {bindings,writes,research}
}
async function choose(page:Page,card:Locator,label:string,option:string){
  await card.locator('label').filter({hasText:label}).locator('.t-select').click()
  await page.locator('.t-select-option:visible').filter({hasText:new RegExp('^'+option)}).click()
  await expect(page.locator('.t-select-option:visible')).toHaveCount(0)
}

test('bindings are separate cards and updating one preserves the others',async({page})=>{
  const {writes}=await setup(page)
  const card=page.locator('.binding-card').first()
  await choose(page,card,'知识库','知识库丙')
  await card.getByRole('button',{name:'保存知识库绑定',exact:true}).click()
  await expect.poll(()=>writes.length).toBe(1)
  expect(writes[0]).toEqual({domain:'one',ids:['kb-b','kb-c']})
  await expect(page.locator('.binding-card')).toHaveCount(3)
  await expect(page.locator('.knowledge-page')).not.toContainText('kb-a')
  await expect(page.locator('.retrieval-test:visible')).toHaveCount(0)
})

test('new cards save independently in the same domain without losing bindings',async({page})=>{
  const {writes,bindings}=await setup(page)
  await page.getByRole('button',{name:'＋ 添加绑定',exact:true}).click()
  const a=page.locator('.binding-card').nth(3)
  await choose(page,a,'业务域','业务域一');await choose(page,a,'知识库','知识库丙')
  await page.getByRole('button',{name:'＋ 添加绑定',exact:true}).click()
  const b=page.locator('.binding-card').nth(4)
  await choose(page,b,'业务域','业务域一');await choose(page,b,'知识库','知识库丁')
  await a.getByRole('button',{name:'保存知识库绑定',exact:true}).click()
  await b.getByRole('button',{name:'保存知识库绑定',exact:true}).click()
  await expect.poll(()=>writes.length).toBe(2)
  expect(new Set(bindings.one)).toEqual(new Set(['kb-a','kb-b','kb-c','kb-d']))
  expect(bindings.two).toEqual(['kb-a'])
})

test('retrieval uses a multiline drawer and the clicked binding only',async({page})=>{
  const {research}=await setup(page)
  await page.locator('.binding-card').first().getByRole('button',{name:'知识检索测试',exact:true}).click()
  const drawer=page.locator('.t-drawer:visible')
  await expect(drawer).toContainText('业务域一 · 知识检索测试')
  await drawer.locator('textarea').fill('查询记录编号\n补充业务背景')
  await drawer.getByRole('button',{name:'开始检索',exact:true}).click()
  await expect(drawer.locator('.knowledge-points')).toContainText('编号规则')
  await expect(drawer.locator('.knowledge-points')).toContainText('按记录编号识别对应实体。')
  expect(research).toEqual([{query:'查询记录编号\n补充业务背景',knowledge_base_ids:['kb-a']}])
  await page.keyboard.press('Escape')
  await expect(drawer).toHaveCount(0)
  await page.locator('.binding-card').nth(1).getByRole('button',{name:'知识检索测试',exact:true}).click()
  await expect(page.locator('.t-drawer:visible textarea')).toHaveValue('')
  await expect(page.locator('.t-drawer:visible .knowledge-points')).toHaveCount(0)
})

test('unbinding one card preserves other knowledge bindings',async({page})=>{
  const {writes}=await setup(page)
  await page.locator('.binding-card').first().getByRole('button',{name:'解除绑定',exact:true}).click()
  await page.locator('.t-dialog:visible').getByRole('button',{name:'解除绑定',exact:true}).click()
  await expect(page.locator('.binding-card')).toHaveCount(2)
  expect(writes).toEqual([{domain:'one',ids:['kb-b']}])
})

test('changing a provider keeps bindings but never reuses an inaccessible old catalog',async({page})=>{
  const {writes}=await setup(page)
  await page.getByRole('button',{name:'连接配置',exact:true}).click()
  await page.locator('.t-dialog:visible').getByRole('button',{name:'保存配置',exact:true}).click()
  await expect(page.locator('.knowledge-page')).toContainText('新实例暂不可访问')
  await expect(page.locator('.binding-card')).toHaveCount(3)
  for(const card of await page.locator('.binding-card').all())await expect(card.getByRole('button',{name:'知识检索测试',exact:true})).toBeDisabled()
  expect(writes).toHaveLength(0)
})
