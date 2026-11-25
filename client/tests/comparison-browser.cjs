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
    await page.goto(origin + '/fixture.html#/scout/compare')
    await page.getByRole('heading', { name: 'Сравнение двух сборов', exact: true }).waitFor()
    const base = page.getByLabel('Ссылка на исходный результат', { exact: true }),
      target = page.getByLabel('Ссылка на более поздний результат', { exact: true }),
      field = page.getByLabel('Сравниваемое поле', { exact: true }),
      check = page.getByRole('button', { name: 'Проверить результаты', exact: true }),
      compare = page.getByRole('button', { name: 'Сравнить поле', exact: true })
    const ids = await page.evaluate(() => ({
      base: window.__scoutTest.comparison.baseId,
      target: window.__scoutTest.comparison.targetId,
    }))
    await base.fill(`https://foreign.example/scout?run=${ids.base}`)
    await target.fill(origin + '/scout?run=' + ids.target)
    assert(await check.isDisabled())
    assert.equal(await page.evaluate(() => window.__scoutTest.comparison.calls.length), 0)
    await base.fill(origin + '/scout?run=' + ids.base)
    await check.click()
    await page.waitForFunction(() => document.querySelector('input[list]')?.value === 'price')
    assert(await compare.isEnabled())
    await field.fill(' price ')
    await compare.click()
    await page
      .getByRole('heading', { name: 'Изменения и непроверенные значения', exact: true })
      .waitFor()
    assert.equal(await field.inputValue(), 'price')
    assert(page.url().includes('field=price'))
    const rows = page.locator('.scout-comparison-changes > li')
    assert.equal(await rows.count(), 1)
    await page.getByRole('heading', { name: 'Изменения цены', exact: true }).waitFor()
    const sources = rows.first().getByRole('table')
    assert.equal(await sources.getByRole('row').count(), 3)
    assert.equal(await sources.getByRole('link').count(), 2)
    assert((await rows.first().innerText()).includes('1250.00 RUB'))
    assert((await rows.first().innerText()).includes('1350.00 RUB'))
    for (const side of ['До', 'После']) {
      const panel = rows.first().getByRole('region', { name: side, exact: true })
      await panel.locator('summary').filter({ hasText: 'Источники значения' }).click()
      await panel.getByRole('link').waitFor()
      assert(
        (await panel.innerText()).includes(
          side === 'До' ? 'https://catalog.example/before' : 'https://catalog.example/after',
        ),
      )
    }
    await page.evaluate(() => (window.__scoutTest.comparison.error = 503))
    await page.getByRole('button', { name: 'Загрузить следующую порцию', exact: true }).click()
    await page.getByRole('button', { name: 'Повторить загрузку порции', exact: true }).waitFor()
    assert.equal(await rows.count(), 1)
    await page.evaluate(() => (window.__scoutTest.comparison.error = 0))
    await page.getByRole('button', { name: 'Повторить загрузку порции', exact: true }).click()
    await page.waitForFunction(
      () => document.querySelectorAll('.scout-comparison-changes>li').length === 2,
    )
    assert((await rows.nth(1).innerText()).includes('EXTERNAL-03'))
    assert.equal(
      await page.getByRole('button', { name: 'Загрузить следующую порцию', exact: true }).count(),
      0,
    )
    await page.getByRole('button', { name: 'Выбрать другое поле', exact: true }).click()
    assert(await field.evaluate((input) => input === document.activeElement))
    await field.fill('zero')
    await compare.click()
    await rows.first().waitFor()
    assert.equal(
      await rows
        .first()
        .getByRole('region', { name: 'До', exact: true })
        .locator('.scout-comparison-value')
        .innerText(),
      '0',
    )
    await field.fill('price')
    await page.evaluate(() => (window.__scoutTest.comparison.uncertain = true))
    await compare.click()
    await rows.first().getByText('Значение не определено надёжно', { exact: true }).waitFor()
    assert((await rows.first().innerText()).includes('Конфликт наблюдений'))
    assert((await rows.first().innerText()).includes('Ошибка преобразования'))
    assert((await rows.first().innerText()).includes('не число'))
    await page.evaluate(() => (window.__scoutTest.comparison.uncertain = false))
    await page.evaluate(() => (window.__scoutTest.comparison.partial = true))
    await compare.click()
    await page
      .getByText(
        'Одна из сторон частичная. Отсутствие объекта в неполном сборе не подтверждает его исчезновение.',
        { exact: true },
      )
      .waitFor()
    await page.getByRole('button', { name: 'Загрузить следующую порцию', exact: true }).click()
    await rows
      .nth(1)
      .getByText('Не подтверждён повторным частичным сбором', { exact: true })
      .waitFor()
    const later = page.getByRole('region', { name: 'Повторный сбор', exact: true })
    await later.getByText('Достигнут потолок страниц; результат частичный.').waitFor()
    for (const [code, label] of [
      ['frontier', 'Достигнут лимит очереди адресов; результат частичный.'],
      ['bytes', 'Достигнут лимит объёма HTML; результат частичный.'],
    ]) {
      await page.evaluate((value) => (window.__scoutTest.comparison.stopReason = value), code)
      await compare.click()
      await later.getByText(label).waitFor()
    }
    await page.evaluate(() => {
      window.__scoutTest.comparison.partial = false
      window.__scoutTest.comparison.empty = true
    })
    await compare.click()
    await page
      .getByText('Список изменений пуст. Смотрите общие счётчики и полноту обеих сторон.', {
        exact: true,
      })
      .waitFor()
    assert.equal(await rows.count(), 0)
    await page.evaluate(() => {
      window.__scoutTest.comparison.empty = false
      window.__scoutTest.comparison.error = 409
    })
    await field.fill('zero')
    await compare.click()
    await page
      .getByText('Нужны запуски одного задания и плана с поддерживаемым явным ключом.', {
        exact: true,
      })
      .waitFor()
    assert.equal(await base.inputValue(), origin + '/scout?run=' + ids.base)
    await page.reload()
    await page.waitForFunction(() => document.querySelector('input[list]')?.value === 'zero')
    assert.equal(await base.inputValue(), origin + '/scout?run=' + ids.base)
    assert.equal(await target.inputValue(), origin + '/scout?run=' + ids.target)
    assert.equal(
      await page.evaluate(() =>
        window.__scoutTest.comparison.calls.some(([url]) => url.endsWith('/compare/')),
      ),
      false,
    )
    await compare.click()
    await rows.first().waitFor()
    await page.evaluate(() => (window.__scoutTest.comparison.error = 410))
    await page.getByRole('button', { name: 'Загрузить следующую порцию', exact: true }).click()
    await page
      .getByText(
        'Комплект результата одной из сторон удалён. Сравнение с пустой таблицей вместо него не выполняется.',
        { exact: true },
      )
      .waitFor()
    assert.equal(await rows.count(), 0)
    await page.evaluate(() => (window.__scoutTest.comparison.error = 403))
    await check.click()
    await page.getByText('Недостаточно прав для просмотра.', { exact: true }).waitFor()
    assert(await compare.isDisabled())
    await page.evaluate(() => {
      window.__scoutTest.comparison.error = 0
      window.__scoutTest.comparison.incompatible = true
    })
    await check.click()
    await page
      .getByText('Выберите завершённые результаты одного задания и одной версии плана.', {
        exact: true,
      })
      .waitFor()
    assert(await compare.isDisabled())
    for (const [title, job, run] of [
      ['Исходный сбор', 'cccccccc-cccc-4ccc-8ccc-cccccccccccc', ids.base],
      ['Повторный сбор', 'dddddddd-dddd-4ddd-8ddd-dddddddddddd', ids.target],
    ]) {
      const links = page.getByRole('region', { name: title, exact: true }).getByRole('link')
      assert.equal(await links.count(), 2)
      for (const link of await links.all()) {
        const query = new URLSearchParams((await link.getAttribute('href')).split('?')[1])
        assert.equal(query.get('job'), job)
        assert.equal(query.get('run'), run)
      }
    }
    await page.evaluate(() => (window.__scoutTest.comparison.incompatible = false))
    await check.click()
    await page.waitForFunction(() => !document.querySelector('input[list]').disabled)
    await page.evaluate(() => (window.__scoutTest.comparison.defer = true))
    await compare.click()
    await page.waitForFunction(() => window.__scoutTest.comparison.release)
    // The read-only form can change via history/navigation while a response is in flight.
    await page.evaluate(
      () => (location.hash = '/scout/compare?target=bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb'),
    )
    await page.evaluate(() => window.__scoutTest.comparison.release())
    await page.waitForTimeout(80)
    assert.equal(await rows.count(), 0)
    await base.fill(origin + '/scout?run=' + ids.base)
    await check.click()
    await page.waitForFunction(() => !document.querySelector('input[list]').disabled)
    await field.fill('price')
    await compare.click()
    await rows.first().waitFor()
    await page.reload()
    await check.waitFor()
    assert((await base.inputValue()).includes(ids.base))
    await page.waitForFunction(() => !document.querySelector('input[list]').disabled)
    await compare.click()
    await rows.first().waitFor()
    for (const theme of ['light', 'dark']) {
      await page.evaluate(
        (value) => document.documentElement.setAttribute('data-theme', value),
        theme,
      )
      for (const width of [1440, 768, 360, 320]) {
        await page.setViewportSize({ width, height: 1100 })
        const size = await page
          .locator('main')
          .evaluate((e) => ({ width: e.clientWidth, scroll: e.scrollWidth }))
        assert(
          size.scroll <= size.width + 2,
          `Comparison overflow ${theme}/${width}: ${JSON.stringify(size)}`,
        )
      }
      await page.screenshot({
        path: path.join(output, `comparison-${theme}-mobile.png`),
        fullPage: true,
      })
    }
    assert.deepEqual(external, [])
    assert.deepEqual(errors, [])
    console.log(
      'Comparison UI passed: manual links, metadata, both Evidence, partial/unkeyed semantics, empty/counts, cursor/retry, 409/410/403, late response, restored selection, themes and 320–1440px',
    )
  } finally {
    await page.close()
    server.closeAllConnections()
    await new Promise((resolve) => server.close(resolve))
  }
}
