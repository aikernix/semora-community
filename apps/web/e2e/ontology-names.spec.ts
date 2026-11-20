import {test,expect} from '@playwright/test'
import {ontologyNames} from '../src/ontologyNames'
import {fieldPairs,mappingCondition} from '../src/relationshipCondition'

test('multiple field mappings generate conditions and preserve complex legacy expressions',()=>{
  const pairs=[{source:'a',target:'x'},{source:'b',target:'y'}]
  expect(mappingCondition('left','right',pairs)).toBe('left.a = right.x AND left.b = right.y')
  expect(fieldPairs('left.a = right.x AND right.y = left.b','left','right')).toEqual(pairs)
  expect(fieldPairs("left.a = right.x AND left.note = 'one AND two'",'left','right')).toBeNull()
})

const configuration={objects:[
  {name:'source_a_table_12345678',table:'public.entries',dataSourceId:'a',properties:[{name:'owner_ref_a5614527',column:'owner_ref'}]},
  {name:'source_b_table_87654321',table:'public.owners',dataSourceId:'b',properties:[{name:'id_a5614527',column:'id'}]},
]}

test('presentation translates original table and column names and roundtrips edits',()=>{
  const names=ontologyNames(configuration)
  const internal='source_a_table_12345678.owner_ref_a5614527 = source_b_table_87654321.id_a5614527'
  const physical='public.entries.owner_ref = public.owners.id'
  expect(names.display(internal)).toBe(physical)
  expect(names.internal(physical)).toBe(internal)
  expect(names.display('缺少 source_a_table_12345678 与 source_b_table_87654321 的关联')).toBe('缺少 public.entries 与 public.owners 的关联')
  expect(names.internal(physical+' AND public.entries.owner_ref IS NOT NULL')).toBe(internal+' AND source_a_table_12345678.owner_ref_a5614527 IS NOT NULL')
})

test('quoted fields and literals are preserved without accidental text replacement',()=>{
  const names=ontologyNames(configuration)
  expect(names.display('"source_a_table_12345678"."owner_ref_a5614527"')).toBe('public.entries.owner_ref')
  const literal="'source_a_table_12345678.owner_ref_a5614527'"
  expect(names.display(literal)).toBe(literal)
  expect(names.display('source_a_table_12345678_extra')).toBe('source_a_table_12345678_extra')
  expect(names.text('缺少source_a_table_12345678的关联')).toBe('缺少public.entries的关联')
})

test('same physical table across sources remains unambiguous when saved',()=>{
  const model=structuredClone(configuration)
  model.objects[1].table='public.entries'
  const names=ontologyNames(model,[{id:'a',name:'source-one'},{id:'b',name:'source-two'}])
  const internal='source_a_table_12345678.owner_ref_a5614527 = source_b_table_87654321.id_a5614527'
  expect(names.display(internal)).toBe('"source-one".public.entries.owner_ref = "source-two".public.entries.id')
  expect(names.internal(names.display(internal))).toBe(internal)
})

test('workbench configuration requirements use original names',async({page})=>{
  const gap={summary:'缺少 source_a_table_12345678 与 source_b_table_87654321 的关联',
    resolution:'配置 source_a_table_12345678.owner_ref_a5614527 = source_b_table_87654321.id_a5614527',
    objects:['source_a_table_12345678','source_b_table_87654321'],field_refs:['source_a_table_12345678.owner_ref_a5614527'],depends_on:[]}
  const run={id:'name-run',session_id:'name-session',domain:'names',question:'查询记录',status:'clarifying',events:[],evidence:[],
    requirement_draft:{summary:'统计记录',clarifications:[],technical_gaps:[gap]}}
  await page.route('**/api/v1/**',route=>{
    const path=new URL(route.request().url()).pathname
    if(path==='/api/v1/sessions')return route.fulfill({json:[{id:'name-session',title:'名称显示',domain:'names',run_count:1}]})
    if(path==='/api/v1/domains')return route.fulfill({json:[{domain:'names',name:'名称展示'}]})
    if(path==='/api/v1/model/status')return route.fulfill({json:{configured:true}})
    if(path==='/api/v1/sessions/name-session/runs')return route.fulfill({json:[run]})
    if(path==='/api/v1/ontology/configurations/names')return route.fulfill({json:configuration})
    if(path==='/api/v1/datasources')return route.fulfill({json:[]})
    return route.fulfill({json:{}})
  })
  await page.goto('/workbench')
  await expect(page.locator('.configuration-notice')).toContainText('缺少 public.entries 与 public.owners 的关联')
  await page.getByRole('button',{name:'查看配置要求'}).click()
  await expect(page.locator('.record-error')).toContainText('public.entries.owner_ref = public.owners.id')
  await expect(page.locator('.record-error')).not.toContainText('12345678')
  await expect(page.locator('.record-error')).not.toContainText('a5614527')
})

test('generated relation IDs stay hidden and physical editor conditions save to original mappings',async({page})=>{
  const condition='source_a_table_12345678.owner_ref_a5614527 = source_b_table_87654321.id_a5614527'
  const relation={name:'r_1234internal',label:'自动发现的关联',from:configuration.objects[0].name,to:configuration.objects[1].name,
    condition,cardinality:'N:1',metadata:{candidateId:'1234internal'}}
  const model={...configuration,domain:'names',name:'名称展示',relations:[relation]}
  let saved:any
  await page.route('**/api/v1/**',route=>{
    const path=new URL(route.request().url()).pathname
    if(path==='/api/v1/ontology/configurations')return route.fulfill({json:[{domain:'names',name:'名称展示'}]})
    if(path==='/api/v1/ontology/configurations/names')return route.fulfill({json:model})
    if(path==='/api/v1/ontology/configurations/names/relationships')return route.fulfill({json:{items:[{id:'1234internal',status:'accepted',relation}]}})
    if(path==='/api/v1/ontology/configurations/names/relationships/1234internal'){
      saved=route.request().postDataJSON();return route.fulfill({json:model})
    }
    if(path==='/api/v1/datasources')return route.fulfill({json:[{id:'a',name:'来源库'},{id:'b',name:'目标库'}]})
    return route.fulfill({json:{}})
  })
  await page.goto('/ontology')
  await page.getByRole('button',{name:'查看关联关系',exact:true}).click()
  await expect(page.locator('.relation-condition')).toContainText('public.entries.owner_ref = public.owners.id')
  await expect(page.locator('.relation-card')).not.toContainText('1234internal')
  await expect(page.locator('.relation-card')).not.toContainText('a5614527')
  await page.getByRole('button',{name:'编辑',exact:true}).click()
  const dialog=page.locator('.t-dialog:visible')
  await expect(dialog.locator('.generated-condition')).toContainText('public.entries.owner_ref = public.owners.id')
  await expect(dialog).not.toContainText('12345678')
  await dialog.getByRole('button',{name:'保存并生效',exact:true}).click()
  await expect.poll(()=>saved?.condition).toBe(condition)
})
