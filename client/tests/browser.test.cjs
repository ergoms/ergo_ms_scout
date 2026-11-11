const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const { pathToFileURL } = require('node:url')

const root = path.resolve(__dirname, '../../../..')
const output = path.join(root, 'virtual_env/cache/tmp/scout-client-browser')
process.env.TEMP = path.join(root, 'virtual_env/cache/tmp')
process.env.TMP = process.env.TEMP

async function main() {
  // Keep generated core auto-import metadata in its normal build directory.
  process.chdir(path.join(root, 'core/client'))
  const { build } = await import(
    pathToFileURL(path.join(root, 'virtual_env/npm/node_modules/vite/dist/node/index.js')).href
  )
  await build({
    configFile: path.join(root, 'core/client/vite.config.js'),
    root: __dirname,
    base: './',
    build: {
      outDir: output,
      emptyOutDir: true,
      rolldownOptions: { input: path.join(__dirname, 'fixture.html') },
    },
  })
  const { chromium } = require(path.join(root, 'virtual_env/npm/node_modules/playwright'))
  const executablePath =
    process.env.ERGO_BROWSER_EXECUTABLE ||
    [
      'C:/Program Files/Google/Chrome/Application/chrome.exe',
      'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
    ].find((file) => fs.existsSync(file))
  const browser = await chromium.launch({
    headless: true,
    ...(executablePath ? { executablePath } : {}),
    args: ['--allow-file-access-from-files'],
  })
  let page
  try {
    await require('./inspector-browser.cjs')(browser, output)
    await require('./visual-browser.cjs')(browser, output)
    page = await browser.newPage({ viewport: { width: 1440, height: 1100 } })
    const errors = []
    page.on('pageerror', (error) => errors.push(error.message))
    await page.goto(pathToFileURL(path.join(output, 'fixture.html')).href)
    const ui = page.locator('.scout-workflow')
    async function checkWidths(name) {
      for (const width of [1440, 768, 360, 320]) {
        await page.setViewportSize({ width, height: 1100 })
        const box = await ui.evaluate((element) => ({
          width: element.clientWidth,
          scroll: element.scrollWidth,
        }))
        assert.ok(
          box.scroll <= box.width + 2,
          `${name}: horizontal overflow at ${width}: ${JSON.stringify(box)}`,
        )
        if (width <= 360) {
          const rail = await ui.locator('.step-rail__list').evaluate((element) => ({
            width: element.clientWidth,
            scroll: element.scrollWidth,
          }))
          assert.ok(rail.scroll <= rail.width + 2, `${name}: hidden steps at ${width}`)
        }
      }
      await page.screenshot({ path: path.join(output, `${name}-mobile.png`), fullPage: true })
      await page.setViewportSize({ width: 1440, height: 1100 })
      await page.screenshot({ path: path.join(output, `${name}-desktop.png`), fullPage: true })
    }
    await ui.getByRole('heading', { name: 'Как подготовим задание?' }).waitFor()
    await checkWidths('modes')

    // Unsupported modes must not send made-up API requests, and must retain the source.
    await ui.locator('.scout-mode').nth(2).click()
    await ui.getByRole('button', { name: 'Далее: источник и правила' }).click()
    await ui.locator('#scout-mode-source').fill('https://events.example/list')
    await ui.locator('#scout-request').fill('Собери названия мероприятий')
    await ui.getByRole('button', { name: 'Перейти к ручным правилам' }).click()
    assert.equal(await ui.locator('#scout-url').inputValue(), 'https://events.example/list')
    assert.equal(await page.evaluate(() => window.__scoutTest.calls.length), 0)

    await ui.locator('#scout-name-0').fill('title')
    await ui.locator('#scout-selector-0').fill('.title')
    await checkWidths('source')
    await ui.getByRole('button', { name: 'Настройки системы', exact: true }).click()
    assert.equal(await page.evaluate(() => window.__scoutTest.systemOpened), 1)
    assert.equal(await ui.locator('#scout-selector-0').inputValue(), '.title')
    await ui.getByRole('button', { name: 'Получить предпросмотр', exact: true }).click()
    await ui.getByRole('heading', { name: 'Предпросмотр готов' }).waitFor()
    assert.equal(
      await page.evaluate(() =>
        window.__scoutTest.calls.some(
          ([method, url]) => method === 'post' && /\/(approve|start)\/$/.test(url),
        ),
      ),
      false,
    )
    await ui.getByRole('button', { name: 'К подтверждению', exact: true }).click()
    await ui.getByRole('heading', { name: 'Подтвердите план сбора' }).waitFor()
    await checkWidths('confirm')
    await ui.getByRole('button', { name: 'К предпросмотру', exact: true }).click()
    await ui.getByRole('heading', { name: 'Предпросмотр готов' }).waitFor()
    await ui.getByRole('button', { name: 'К подтверждению', exact: true }).click()
    await ui.getByRole('button', { name: 'Подтвердить план и запустить', exact: true }).click()
    await ui.getByRole('button', { name: 'Пауза', exact: true }).waitFor()
    const mutations = await page.evaluate(() =>
      window.__scoutTest.calls
        .filter(([method, url]) => method === 'post' && /\/(approve|start)\/$/.test(url))
        .map(([, url]) => url.split('/').at(-2)),
    )
    assert.deepEqual(mutations, ['approve', 'start'])
    await page.evaluate(() => {
      window.__scoutTest.networkDown = true
    })
    await ui.getByRole('alert').filter({ hasText: 'Не удалось обновить состояние' }).waitFor()
    assert.equal(await ui.getByRole('button', { name: 'Пауза', exact: true }).isDisabled(), true)
    await page.evaluate(() => {
      window.__scoutTest.networkDown = false
    })
    await ui.getByRole('button', { name: 'Повторить', exact: true }).click()
    await ui.getByRole('button', { name: 'Пауза', exact: true }).waitFor({ state: 'visible' })
    await ui
      .getByRole('alert')
      .filter({ hasText: 'Не удалось обновить состояние' })
      .waitFor({ state: 'hidden' })

    // A pause request is pending until the server confirms the paused state.
    await ui.getByRole('button', { name: 'Пауза', exact: true }).click()
    await ui.getByRole('heading', { name: 'Останавливаем выдачу новых задач' }).waitFor()
    assert.equal(await ui.getByRole('button', { name: 'Продолжить', exact: true }).count(), 0)
    await page.evaluate(() => window.__scoutTest.setStatus('paused'))
    await ui.getByRole('button', { name: 'Продолжить', exact: true }).waitFor()
    await ui.getByRole('button', { name: 'Продолжить', exact: true }).click()

    await page.evaluate(() => {
      window.__scoutTest.run.record_count = 205
      window.__scoutTest.setStatus('completed')
    })
    await ui.getByRole('heading', { name: 'Завершён', exact: true }).waitFor()
    await ui
      .getByRole('button', { name: 'Происхождение поля «title»: Событие 1', exact: true })
      .click()
    await ui.getByRole('heading', { name: 'Откуда взялось значение: title' }).waitFor()
    assert.match(await ui.locator('.scout-evidence').innerText(), /events\.example/)
    assert.deepEqual(
      await page.evaluate(() =>
        window.__scoutTest.calls
          .filter(([method, url, params]) => method === 'get' && url.endsWith('/records/') && params.order !== 'recent')
          .map(([, , params]) => params),
      ),
      [{ limit: 200 }, { limit: 200, cursor: 'record-200' }],
    )
    assert.equal(
      await ui.getByRole('button', { name: 'Происхождение поля «count»: 0', exact: true }).count(),
      10,
    )
    const routeBefore = await page.evaluate(() => location.hash)
    const pageTwo = ui.locator('.ergo-pagination').getByRole('button', { name: '2', exact: true })
    await pageTwo.click()
    await ui
      .getByRole('button', { name: 'Происхождение поля «title»: Событие 11', exact: true })
      .waitFor()
    const routeAfter = await page.evaluate(() => location.hash)
    for (const key of ['job=', 'run=']) {
      assert.match(routeBefore, new RegExp(key))
      assert.match(routeAfter, new RegExp(key))
    }

    for (const theme of ['Тёмная', 'Светлая']) {
      await ui.getByRole('button', { name: 'Тема оформления', exact: true }).click()
      await page.getByRole('link', { name: theme, exact: true }).press('Enter')
      assert.equal(
        await page.locator('html').getAttribute('data-bs-theme'),
        theme === 'Тёмная' ? 'dark' : 'light',
      )
      await checkWidths(theme === 'Тёмная' ? 'result-dark' : 'result-light')
    }
    for (const width of [1440, 768, 360, 320]) {
      await page.setViewportSize({ width, height: 1100 })
      const box = await ui.evaluate((element) => ({
        width: element.clientWidth,
        scroll: element.scrollWidth,
      }))
      assert.ok(
        box.scroll <= box.width + 2,
        `Horizontal overflow at ${width}: ${JSON.stringify(box)}`,
      )
    }
    await page.setViewportSize({ width: 1440, height: 1100 })
    await page.screenshot({ path: path.join(output, 'result-desktop.png'), fullPage: true })

    // A saved job can contain multiple URLs and rule kinds that the editor cannot represent.
    await page.evaluate(() => {
      const fixture = window.__scoutTest
      const jobId = '33333333-3333-4333-8333-333333333333'
      const runId = '44444444-4444-4444-8444-444444444444'
      const revision = {
        ...fixture.job.revision,
        plan_hash: 'multi-source-plan',
        seed_urls: ['https://events.example/a', 'https://events.example/b'],
        fields: [
          ...fixture.job.revision.fields,
          { name: 'count', kind: 'css', selector: '.count', scope: 'page', many: false, conversion: { type: 'integer' } },
        ],
        identity: { kind: 'sku', namespace: 'events', field: 'title' },
        proxy_profile_revision_public_id: '66666666-6666-4666-8666-666666666666',
        limits: { max_bytes: 1000000, timeout_seconds: 20 },
        crawl: {
          ...fixture.job.revision.crawl,
          mode: 'site',
          allowed_hosts: ['events.example'],
          visit: [
            { kind: 'contains', pattern: 'featured' },
            { kind: 'prefix', pattern: 'https://events.example/b' },
          ],
          extract_on: [
            { kind: 'prefix', pattern: 'https://events.example/a' },
            { kind: 'contains', pattern: 'ticket' },
          ],
          preview_urls: ['https://events.example/sample-a', 'https://events.example/sample-b'],
          drop_query: ['tracking_id'],
          sitemap_url: 'https://events.example/sitemap.xml',
        },
      }
      fixture.job = { ...fixture.job, public_id: jobId, revision }
      fixture.run = {
        ...fixture.run,
        public_id: runId,
        job_public_id: jobId,
        plan_hash: revision.plan_hash,
        crawl: revision.crawl,
        approved: false,
        status: 'awaiting_confirmation',
        record_count: 0,
      }
      location.hash = `/?job=${jobId}&run=${runId}`
    })
    const openConfirmation = ui.getByRole('button', { name: 'К подтверждению', exact: true })
    const confirmationHeading = ui.getByRole('heading', { name: 'Подтвердите план сбора' })
    await openConfirmation.or(confirmationHeading).waitFor()
    if (await openConfirmation.isVisible()) await openConfirmation.click()
    await confirmationHeading.waitFor()
    const plan = ui.locator('.scout-plan--expanded')
    const planText = await plan.innerText()
    for (const text of [
      'https://events.example/a',
      'https://events.example/b',
      'содержит featured',
      'начинается с https://events.example/b',
      'содержит ticket',
      'https://events.example/sample-b',
      'tracking_id',
      '1000000',
    ]) {
      assert.ok(planText.includes(text), `Saved confirmation plan is missing ${text}`)
    }
    const seedAddresses = plan.getByText('https://events.example/b', { exact: true })
    assert.equal(await seedAddresses.count(), 1)
    assert.equal(
      await ui
        .getByRole('button', { name: 'Подтвердить план и запустить', exact: true })
        .isEnabled(),
      true,
    )
    await checkWidths('confirm-multi-source')

    // Previewing an unchanged saved job reuses its revision and fetches the full preview.
    await ui.getByRole('button', { name: 'К предпросмотру', exact: true }).click()
    await ui.getByRole('button', { name: 'Изменить правила', exact: true }).click()
    await page.evaluate(() => {
      window.__scoutTest.calls.length = 0
      window.__scoutTest.deferPreview = true
    })
    const prepare = ui.getByRole('button', { name: 'Получить предпросмотр', exact: true })
    await prepare.click()
    await page.waitForFunction(() => window.__scoutTest.calls.some(([, , params]) => params?.summary === 1))
    await page.evaluate(() => {
      window.__scoutTest.setStatus('awaiting_confirmation')
      window.__scoutTest.deferPreview = false
    })
    await ui.getByRole('button', { name: 'К подтверждению', exact: true }).waitFor()
    assert.equal(
      await page.evaluate(() => window.__scoutTest.calls.filter(([method]) => method === 'patch').length),
      0,
    )

    await ui.getByRole('button', { name: 'Изменить правила', exact: true }).click()
    await ui.locator('.scout-crawl-editor > summary').click()
    await ui.locator('#scout-visit').fill('featured-updated')
    await prepare.click()
    await ui.getByRole('button', { name: 'К подтверждению', exact: true }).waitFor()
    const changed = await page.evaluate(() => window.__scoutTest.calls.find(([method]) => method === 'patch')[2])
    assert.deepEqual(changed.seed_urls, ['https://events.example/a', 'https://events.example/b'])
    assert.deepEqual(changed.crawl.visit, [
      { kind: 'contains', pattern: 'featured-updated' },
      { kind: 'prefix', pattern: 'https://events.example/b' },
    ])
    assert.deepEqual(changed.crawl.extract_on, [
      { kind: 'prefix', pattern: 'https://events.example/a' },
      { kind: 'contains', pattern: 'ticket' },
    ])
    assert.deepEqual(changed.crawl.preview_urls, ['https://events.example/sample-a', 'https://events.example/sample-b'])
    assert.deepEqual(changed.crawl.drop_query, ['tracking_id'])
    assert.equal(changed.crawl.sitemap_url, 'https://events.example/sitemap.xml')
    assert.deepEqual(changed.fields[1].conversion, { type: 'integer' })
    assert.deepEqual(changed.identity, { kind: 'sku', namespace: 'events', field: 'title' })
    assert.equal(changed.proxy_profile_revision_public_id, '66666666-6666-4666-8666-666666666666')

    // Status polling continues without starting a second request for the recent records.
    await page.evaluate(() => {
      const fixture = window.__scoutTest
      const runId = '55555555-5555-4555-8555-555555555555'
      fixture.calls.length = 0
      fixture.deferNextRecords = true
      fixture.pauseStatus = 'paused'
      fixture.run = { ...fixture.run, public_id: runId, status: 'running', record_count: 25 }
      location.hash = `/?job=${fixture.job.public_id}&run=${runId}`
    })
    await page.waitForFunction(() => window.__scoutTest.releaseRecords !== null)
    assert.equal(await ui.getAttribute('aria-busy'), 'true')
    await page.waitForFunction(() => window.__scoutTest.calls.some(([, , params]) => params?.summary === 1))
    assert.deepEqual(
      await page.evaluate(() =>
        window.__scoutTest.calls
          .filter(([method, url]) => method === 'get' && url.endsWith('/records/'))
          .map(([, , params]) => params),
      ),
      [{ limit: 20, order: 'recent' }],
    )
    await ui.getByRole('button', { name: 'Пауза', exact: true }).click()
    await ui.getByRole('button', { name: 'Продолжить', exact: true }).waitFor()
    await page.evaluate(() => window.__scoutTest.releaseRecords())
    await ui.getByText('Последние 20 из 25', { exact: true }).waitFor()
    assert.equal(
      await ui.getByRole('button', { name: 'Происхождение поля «title»: Событие 1', exact: true }).count(),
      0,
    )
    await page.waitForFunction(
      () => document.querySelector('.scout-workflow')?.getAttribute('aria-busy') === 'false',
    )
    assert.equal(
      await ui.getByRole('button', { name: 'Новое задание', exact: true }).isEnabled(),
      true,
    )

    assert.deepEqual(errors, [])
    console.log(
      'Scout browser checks passed: mode availability, draft, system event, preview, approval/start, full saved plan, pending and immediate pause, all records, provenance, pagination, themes, 320–1440px.',
    )
  } catch (error) {
    if (page) {
      await page.screenshot({ path: path.join(output, 'failure.png'), fullPage: true })
      const state = await page.evaluate(() => ({
        calls: window.__scoutTest?.calls,
        hash: location.hash,
        text: document.body.innerText,
      }))
      fs.writeFileSync(path.join(output, 'failure.json'), JSON.stringify(state, null, 2))
    }
    throw error
  } finally {
    await browser.close()
  }
}
main().catch((error) => {
  console.error(error.message)
  process.exitCode = 1
})
