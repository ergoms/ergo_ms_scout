const assert = require('node:assert/strict'),
  path = require('node:path'),
  fs = require('node:fs'),
  http = require('node:http')
module.exports = async (browser, output) => {
  const server = http.createServer((req, res) => {
    const name = decodeURIComponent(new URL(req.url, 'http://localhost').pathname)
    const file = path.resolve(output, '.' + name)
    if (!file.startsWith(path.resolve(output) + path.sep)) {
      res.writeHead(403).end()
      return
    }
    fs.readFile(file, (err, data) => {
      if (err) {
        res.writeHead(404).end()
        return
      }
      res.setHeader(
        'Content-Type',
        file.endsWith('.html')
          ? 'text/html'
          : file.endsWith('.js')
            ? 'application/javascript'
            : file.endsWith('.css')
              ? 'text/css'
              : 'application/octet-stream',
      )
      res.end(data)
    })
  })
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve))
  const origin = `http://127.0.0.1:${server.address().port}`,
    page = await browser.newPage({ viewport: { width: 1440, height: 1100 } }),
    errors = [],
    external = []
  page.on('pageerror', (e) => errors.push(e.message))
  page.on('request', (req) => {
    if (req.url().includes('catalog.example') || req.url().includes('foreign.example'))
      external.push(req.url())
  })
  try {
    const job = 'dddddddd-dddd-4ddd-8ddd-dddddddddddd'
    const baseId = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
    const targetId = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb'
    const otherJob = '77777777-7777-4777-8777-777777777777'
    await page.goto(origin + `/fixture.html#/scout/compare?job=${job}&target=${targetId}`)
    const history = page.locator('.scout-history')
    const rows = history.locator('tbody tr')
    const apply = history.getByRole('button', {
      name: 'Использовать выбранные результаты',
      exact: true,
    })
    await rows.first().waitFor()
    assert.equal(await rows.count(), 3)
    assert(await history.locator(`[data-run="${targetId}"] input`).nth(1).isChecked())
    assert(await apply.isDisabled())
    for (const id of [
      'eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee',
      'ffffffff-ffff-4fff-8fff-ffffffffffff',
    ]) {
      for (const input of await history.locator(`[data-run="${id}"] input`).all())
        assert(await input.isDisabled())
    }
    await page.evaluate(() => {
      window.__scoutTest.comparison.history.error = 503
    })
    await history.getByRole('button', { name: 'Загрузить следующие запуски', exact: true }).click()
    await history.getByRole('alert').waitFor()
    assert.equal(await rows.count(), 3)
    await page.evaluate(() => {
      window.__scoutTest.comparison.history.error = 0
    })
    await history.getByRole('button', { name: 'Повторить загрузку истории', exact: true }).click()
    await history.locator(`[data-run="${baseId}"]`).waitFor()
    assert.equal(await rows.count(), 4)
    await history.locator(`[data-run="${baseId}"] input`).first().focus()
    await page.keyboard.press('Space')
    assert(await apply.isEnabled())
    await apply.click()
    const compare = page.getByRole('button', { name: 'Сравнить поле', exact: true })
    await page.waitForFunction(() => !document.querySelector('input[list]').disabled)
    assert(await compare.isEnabled())
    assert(
      (
        await page.getByLabel('Ссылка на исходный результат', { exact: true }).inputValue()
      ).includes(baseId),
    )
    assert(
      (
        await page.getByLabel('Ссылка на более поздний результат', { exact: true }).inputValue()
      ).includes(targetId),
    )
    assert.equal(await page.getByLabel('Сравниваемое поле', { exact: true }).inputValue(), 'price')
    await compare.click()
    await page.locator('.scout-comparison-changes > li').first().waitFor()
    for (const theme of ['light', 'dark']) {
      await page.evaluate((value) => window.__scoutTest.setTheme(value), theme)
      for (const width of [1440, 768, 360, 320]) {
        await page.setViewportSize({ width, height: 1100 })
        const size = await page
          .locator('main')
          .evaluate((e) => ({ width: e.clientWidth, scroll: e.scrollWidth }))
        assert(
          size.scroll <= size.width + 2,
          `History overflow ${theme}/${width}: ${JSON.stringify(size)}`,
        )
        for (const input of await history.locator('input[type="radio"]').all()) {
          const box = await input.boundingBox()
          assert(
            box && box.x >= 0 && box.x + box.width <= width,
            `History choice is offscreen at ${width}`,
          )
        }
      }
      await page.screenshot({
        path: path.join(output, `run-history-${theme}-mobile.png`),
        fullPage: true,
      })
    }
    await page.setViewportSize({ width: 1440, height: 1100 })
    await page.evaluate(() => {
      window.__scoutTest.comparison.history.defer = true
    })
    await history.getByRole('button', { name: 'Обновить историю', exact: true }).click()
    await page.waitForFunction(() => window.__scoutTest.comparison.history.release)
    await history.getByLabel('Задание', { exact: true }).selectOption(otherJob)
    await history.getByText('У этого задания пока нет запусков.', { exact: true }).waitFor()
    await page.evaluate(() => window.__scoutTest.comparison.history.release())
    await page.waitForTimeout(60)
    assert.equal(await rows.count(), 0)
    assert.equal(await history.getByLabel('Задание', { exact: true }).inputValue(), otherJob)
    assert.deepEqual(errors, [])
    assert.deepEqual(external, [])
    console.log(
      'Run history UI passed: current target, blocked/expired rows, cursor/retry, selection and comparison, stale response, empty job, themes and 320–1440px',
    )
  } finally {
    await page.close()
    server.closeAllConnections()
    await new Promise((resolve) => server.close(resolve))
  }
}
