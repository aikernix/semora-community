import { test, expect } from '@playwright/test'

test.setTimeout(45_000)

test('database products expose their real connection options', async ({ page }) => {
  await page.goto('/datasources')
  await expect(page.getByRole('heading', { name: '数据源', exact: true })).toBeVisible()
  await page.getByRole('button', { name: '添加数据源', exact: true }).click()
  const dialog = page.locator('.t-dialog:visible').filter({ hasText: '添加数据源' })
  const products = dialog.locator('label').filter({ hasText: '数据库类型' }).locator('.t-select')
  await products.click()
  await page.getByText('Oracle', { exact: true }).last().click()
  await expect(dialog.getByText('服务名（Service Name）', { exact: true })).toBeVisible()
  await dialog.getByText('高级连接配置', { exact: true }).click()
  await products.click()
  await page.getByText('OceanBase', { exact: true }).last().click()
  await expect(dialog.locator('label').filter({ hasText: '兼容模式' })).toBeVisible()
  await products.click()
  await page.getByText('自定义 JDBC', { exact: true }).last().click()
  await expect(dialog.locator('label').filter({ hasText: 'JDBC 驱动' }).first()).toBeVisible()
  await expect(dialog.getByText('JDBC 连接串', { exact: true })).toBeVisible()
})

test('driver management rejects a corrupt bundle without publishing it', async ({ page }) => {
  await page.goto('/datasources')
  await page.getByRole('button', { name: '驱动管理', exact: true }).click()
  const dialog = page.locator('.t-dialog:visible').filter({ hasText: '管理员 JDBC 驱动' })
  await expect(dialog.getByText(/ojdbc11-23\.6\.0\.24\.10/)).toBeVisible()
  await dialog.locator('textarea').fill(JSON.stringify({ name: 'invalid fixture', driver_class: 'fixture.Driver', enabled: false }))
  await dialog.locator('input[type=file]').setInputFiles({ name: 'corrupt.jar', mimeType: 'application/java-archive', buffer: Buffer.from('not a jar') })
  await dialog.getByRole('button', { name: '上传驱动包', exact: true }).click()
  await expect(page.locator('.datasource-page > .t-alert')).toContainText('无效 JAR 文件')
  await expect(dialog.locator('.driver-row').filter({ hasText: 'invalid fixture' })).toHaveCount(0)
})
