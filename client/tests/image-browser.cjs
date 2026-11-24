const assert = require('node:assert/strict')
const fs = require('node:fs/promises')
const path = require('node:path')
const { pathToFileURL } = require('node:url')

module.exports = async (browser, output) => {
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } })
  try {
    await page.goto(pathToFileURL(path.join(output, 'fixture.html')).href + '#/product')
    const ui = page.locator('.scout-workflow')
    await ui.locator('.scout-mode').nth(1).click()
    await ui.getByRole('button', { name: 'Далее: источник и правила' }).click()
    await ui.locator('#scout-visual-url').fill('https://books.toscrape.com/')
    await ui.getByRole('button', { name: 'Загрузить страницу', exact: true }).click()

    const first = ui.locator('[role=treeitem][data-node=n1]')
    const image = ui.locator('[role=treeitem][data-node=n61]')
    const button = ui.getByRole('button', { name: 'Скачать изображение', exact: true })
    await first.press('ArrowRight')
    await image.waitFor()
    await first.press('ArrowRight')
    await page.keyboard.press('ArrowDown')
    await page.keyboard.press('ArrowDown')
    assert(await image.evaluate((node) => document.activeElement === node))
    await page.keyboard.press('Enter')
    await button.waitFor()
    const downloads = () =>
      page.evaluate(() => window.__scoutTest.calls.filter(([method]) => method === 'download'))
    assert.deepEqual(await downloads(), [])
    const frame = page.frameLocator('.scout-snapshot-frame iframe')
    const placeholder = frame.locator('span[data-scout-image]')
    assert.equal(await placeholder.textContent(), 'Изображение')
    assert.equal(await placeholder.evaluate((node) => getComputedStyle(node).cursor), 'pointer')
    assert.equal(await frame.locator('img,[src]').count(), 0)
    await first.click()
    await button.waitFor({ state: 'detached' })
    await placeholder.click()
    await button.waitFor()

    const [file] = await Promise.all([page.waitForEvent('download'), button.press('Enter')])
    assert.equal(file.suggestedFilename(), 'scout-n61.png')
    assert.match((await fs.readFile(await file.path())).toString('hex'), /^89504e470d0a1a0a/)
    assert.deepEqual(await downloads(), [
      [
        'download',
        'scout/visual-snapshots/visual-1/image/',
        {
          node_id: 'n61',
          tree_version: 'dom-v1:' + 'a'.repeat(64),
        },
      ],
    ])
    await page.evaluate(() => {
      window.__scoutTest.visual.imageHeaders = {
        'content-disposition': 'attachment; filename="cover.png"',
      }
    })
    const [named] = await Promise.all([page.waitForEvent('download'), button.click()])
    assert.equal(named.suggestedFilename(), 'cover.png')

    const unexpected = []
    page.on('download', (file) => unexpected.push(file.suggestedFilename()))
    await page.evaluate(() => {
      window.__scoutTest.visual.imageMime = 'text/html'
    })
    await button.click()
    await ui.getByText('Для скачивания поддерживаются JPEG, PNG, GIF и WebP.').waitFor()
    await page.evaluate(() => {
      window.__scoutTest.visual.imageError = 409
    })
    await button.click()
    await ui.getByText('Снимок изменился или истёк.', { exact: false }).waitFor()

    await page.evaluate(() => {
      Object.assign(window.__scoutTest.visual, { imageError: 0, imageMime: '', defer: 'image' })
    })
    await button.click()
    const busy = ui.getByRole('button', { name: 'Скачиваем изображение…' })
    assert(await busy.isDisabled())
    await first.click()
    await button.waitFor({ state: 'detached' })
    await page.evaluate(() => window.__scoutTest.visual.release())
    await image.press('Enter')
    await button.waitFor()
    await page.evaluate(() => {
      window.__scoutTest.visual.defer = 'image'
    })
    await button.click()
    await busy.waitFor()
    await ui.locator('.scout-visual-source button').click()
    await first.waitFor()
    await page.evaluate(() => window.__scoutTest.visual.release())
    await first.press('ArrowRight')
    await image.press('Enter')
    await button.waitFor()
    assert.deepEqual(unexpected, [])
    console.log(
      'Selected image download passed: keyboard, binary file, errors and stale responses.',
    )
    await checkImageCollection(page, ui, frame)
  } finally {
    await page.close()
  }
}

async function checkImageCollection(page, ui, frame) {
  const externalRequests = []
  await page.route('https://books.toscrape.com/**', (route) => {
    externalRequests.push(route.request().url())
    return route.abort()
  })
  await page.evaluate(() => {
    window.__scoutTest.calls.length = 0
    window.__scoutTest.visual.styled = true
  })
  // Chromium reports a CSP denial as a request failure before it reaches routing.
  const [blockedStyle] = await Promise.all([
    page.waitForEvent('requestfailed', (request) =>
      request.url() === 'https://books.toscrape.com/blocked-style.png',
    ),
    ui.locator('.scout-visual-source button').click(),
  ])
  assert.match(blockedStyle.failure()?.errorText || '', /^(csp|net::ERR_BLOCKED_BY_CSP)$/)
  const photo = frame.getByRole('img', { name: 'Cover', exact: true })
  assert.equal(await photo.getAttribute('data-scout-node'), 'n61')
  assert.equal(await photo.getAttribute('data-scout-image'), 'preview')
  assert.deepEqual(
    await photo.evaluate(async (node) => {
      await node.decode()
      const style = getComputedStyle(node)
      const card = getComputedStyle(node.parentElement)
      const { x, y, width, height } = node.getBoundingClientRect()
      const nativeHit = document.elementFromPoint(x + width / 2, y + height / 2)
      return {
        width,
        height,
        padding: style.padding,
        border: style.borderWidth,
        cursor: style.cursor,
        grid: card.display,
        gap: card.columnGap,
        nativeHit: nativeHit?.getAttribute('data-scout-node'),
      }
    }),
    {
      width: 96,
      height: 72,
      padding: '0px',
      border: '0px',
      cursor: 'pointer',
      grid: 'grid',
      gap: '12px',
      nativeHit: 'n62',
    },
  )
  const decoration = frame.locator('.photo-decoration')
  const lastCheck = () =>
    page.evaluate(
      () => window.__scoutTest.calls.filter((call) => call[1].endsWith('/check/')).at(-1)[2].field,
    )
  assert.equal(await page.evaluate(() => window.__snapshotScriptRan), undefined)
  await decoration.click()
  await ui.locator('#scout-visual-name').fill('cover')
  const verify = ui.getByRole('button', { name: 'Проверить правило', exact: true })
  await verify.click()
  assert.equal((await lastCheck()).node_id, 'n61')
  await ui
    .getByText('Во время сбора Scout сохранит выбранные изображения.', { exact: false })
    .waitFor()
  await ui.getByRole('button', { name: 'Принять поле', exact: true }).click()
  const saved = ui.locator('.scout-visual-fields li').filter({ hasText: 'cover' })
  await saved.getByText(/Изображение/).waitFor()
  const edit = saved.getByRole('button', { name: 'Изменить', exact: true })
  await edit.click()
  await verify.click()
  assert.equal((await lastCheck()).value_source, 'image')
  await ui.getByRole('button', { name: 'Сохранить изменения', exact: true }).click()

  // Selecting a text node while editing an image asks the server to infer the new source.
  await edit.click()
  await frame.locator('h3').first().click()
  await verify.click()
  assert.equal((await lastCheck()).value_source, undefined)
  await ui.getByRole('button', { name: 'Сохранить изменения', exact: true }).click()
  assert.equal(await saved.getByText(/Изображение/).count(), 0)
  await edit.click()
  await decoration.click()
  await verify.click()
  assert.equal((await lastCheck()).node_id, 'n61')
  await ui
    .getByText('Во время сбора Scout сохранит выбранные изображения.', { exact: false })
    .waitFor()
  await ui.getByRole('button', { name: 'Сохранить изменения', exact: true }).click()
  await ui.getByRole('button', { name: /Получить предпросмотр/ }).click()
  await ui.getByRole('heading', { name: 'Предпросмотр готов' }).waitFor()
  await ui.getByText('Будет скачано во время сбора', { exact: true }).first().waitFor()
  const field = await page.evaluate(() => window.__scoutTest.job.revision.fields[0])
  assert.deepEqual(field, {
    name: 'cover',
    kind: 'css',
    selector: 'img',
    scope: 'page',
    many: false,
    value_source: 'image',
  })
  assert.equal(
    await ui.getByRole('button', { name: 'Скачать изображение', exact: true }).count(),
    0,
  )
  assert.equal(
    await page.evaluate(() => window.__scoutTest.calls.some(([method]) => method === 'download')),
    false,
  )

  await ui.getByRole('button', { name: 'К подтверждению', exact: true }).click()
  await ui.getByRole('button', { name: 'Подтвердить план и запустить', exact: true }).click()
  await ui.getByRole('button', { name: 'Пауза', exact: true }).waitFor()
  await page.evaluate(() => {
    window.__scoutTest.setStatus('completed')
  })
  await ui.getByRole('heading', { name: 'Завершён', exact: true }).waitFor()
  await ui.getByText('Файл сохранён', { exact: true }).first().waitFor()
  await ui.getByText('Не удалось скачать файл', { exact: true }).first().waitFor()
  await ui.getByText('Пропущено из-за лимита сбора', { exact: true }).first().waitFor()
  const resultButton = ui.getByRole('button', { name: 'Скачать изображение', exact: true }).first()
  const [file] = await Promise.all([page.waitForEvent('download'), resultButton.click()])
  assert.match((await fs.readFile(await file.path())).toString('hex'), /^89504e470d0a1a0a/)
  const downloads = await page.evaluate(() =>
    window.__scoutTest.calls.filter(([method]) => method === 'download'),
  )
  assert.deepEqual(downloads, [
    [
      'download',
      'scout/runs/22222222-2222-4222-8222-222222222222/images/aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa/',
      {},
    ],
  ])
  assert.deepEqual(externalRequests, [])
  await page.evaluate(() => {
    window.__scoutTest.visual.imageError = 410
  })
  await resultButton.click()
  await ui.getByText('Файл отсутствует или срок его хранения истёк.', { exact: true }).waitFor()
  console.log(
    'Image collection passed: click through photo decoration, saved image field, preview, approval and result download.',
  )
}
