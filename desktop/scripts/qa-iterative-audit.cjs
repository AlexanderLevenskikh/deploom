// Real components with synthetic IPC; no migration or provider call.
// Uses ordinary Playwright: the Browser plugin was unavailable in the implementation session.
const { chromium } = require(process.env.DEPLOOM_PLAYWRIGHT_MODULE || 'playwright')
const assert = require('node:assert/strict')
const { resolve, join } = require('node:path')
const { mkdirSync } = require('node:fs')
const output = process.env.DEPLOOM_QA_OUTPUT || resolve(__dirname, '../.artifacts/iterative-audit')
mkdirSync(output, { recursive: true })
;(async () => {
 const browser = await chromium.launch({ channel: process.env.DEPLOOM_QA_BROWSER_CHANNEL || (process.platform === 'win32' ? 'msedge' : undefined), headless: true })
 const page = await browser.newPage({ viewport: { width: 1440, height: 1100 } })
 const errors = []
 page.on('pageerror', error => errors.push(error.message))
 const base = (process.env.DEPLOOM_QA_URL || 'http://127.0.0.1:5179/monitor-harness.html') + '?audit='
 for (const mode of ['initial','unknown','final']) {
  await page.goto(base + mode)
  const summary = page.getByTestId('audit-goals')
  await summary.waitFor()
  await page.waitForFunction(() => document.querySelector('[data-testid="audit-goals"]')?.textContent.includes('92.7') || document.querySelector('[data-testid="audit-goals"]')?.textContent.includes('68.0'))
  const text = await summary.innerText()
  if (mode === 'initial') { assert.ok(text.includes('Critical: shell-quote')); assert.ok(text.includes('Цели ещё не выполнены')); assert.equal(await summary.locator('.status-dot.danger').count(), 3) }
  if (mode === 'unknown') { assert.ok(text.includes('Нет подтверждения')); assert.equal(await summary.locator('.status-dot.success').count(), 0) }
  if (mode === 'final') {
   assert.ok(text.includes('Выбранные цели выполнены')); assert.ok(text.includes('89/96')); assert.ok(text.includes('Обязательные версии'))
   await page.getByTestId('delivery-result').waitFor(); assert.ok((await page.getByTestId('delivery-result').innerText()).includes('refactor(icons)'))
  }
  await page.getByRole('button', { name: 'Рабочая директория итерации', exact: true }).first().click()
  assert.equal(await page.locator('html').getAttribute('data-opened-path'), 'C:/demo/run/trial/workspace')
  await page.getByRole('button', { name: 'Открыть артефакты', exact: true }).click()
  assert.equal(await page.locator('html').getAttribute('data-opened-path'), 'C:/demo/run/reports')
  await summary.scrollIntoViewIfNeeded()
  await page.screenshot({ path: join(output, 'audit-' + mode + '.png') })
 }
 await page.getByRole('button', { name: 'EN', exact: true }).click()
 assert.ok((await page.getByTestId('audit-goals').innerText()).includes('Selected goals met'))
 await page.evaluate(() => { document.documentElement.dataset.theme = 'light' })
 await page.screenshot({ path: join(output, 'audit-light.png') })
 await page.setViewportSize({ width: 640, height: 950 })
 await page.getByTestId('audit-goals').scrollIntoViewIfNeeded()
 const overflow = await page.getByTestId('audit-goals').evaluate(el => el.scrollWidth > el.clientWidth)
 assert.equal(overflow, false, 'audit summary must fit narrow windows')
 assert.deepEqual(errors, [])
 await browser.close()
 console.log('Browser QA OK: initial Critical, unknown, final goals/commits, RU/EN, adjacent directory links and 640px layout; no page errors')
})().catch(error => { console.error(error); process.exitCode = 1 })
