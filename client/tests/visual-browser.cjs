const assert = require('node:assert/strict')
const path = require('node:path')
const { pathToFileURL } = require('node:url')

module.exports = async function checkVisual(browser, output) {
  const page = await browser.newPage({ viewport: { width: 1440, height: 1100 } })
  try {
    await page.goto(pathToFileURL(path.join(output, 'fixture.html')).href)
    const ui = page.locator('.scout-workflow')
    await ui.locator('.scout-mode').nth(1).click()
    await ui.getByRole('button', { name: 'Далее: источник и правила' }).click()
    await ui.locator('#scout-visual-url').fill('https://books.toscrape.com/')
    const load = ui.getByRole('button', { name: 'Загрузить страницу', exact: true })
    await load.click()
    const frame = page.frameLocator('.scout-snapshot-frame iframe')
    await frame.locator('article').first().waitFor()
    assert.equal(await ui.locator('iframe').getAttribute('sandbox'), 'allow-same-origin')
    assert.equal(await page.evaluate(() => window.__snapshotScriptRan), undefined)
    await ui.locator('#scout-visual-cards').check()
    await frame.locator('p').first().click()
    await ui
      .locator('.scout-visual-ancestors')
      .getByRole('button', { name: 'article', exact: true })
      .click()
    await ui.getByText('Найдено строк: 20', { exact: true }).waitFor()
    await ui.getByRole('button', { name: 'Принять карточку', exact: true }).click()
    await ui.locator('#scout-visual-name').fill('price')
    await frame.locator('p').first().click()
    await ui.getByText('Совпадений в примерах: 20', { exact: true }).waitFor()
    assert.equal(await ui.locator('#scout-visual-css').inputValue(), '.price_color')
    await ui.locator('.scout-snapshot-highlights rect').nth(19).waitFor()

    await ui.locator('#scout-visual-css').fill('.absent')
    await ui.getByRole('button', { name: 'Проверить CSS', exact: true }).click()
    await ui.getByText('Совпадений в примерах: 0', { exact: true }).waitFor()
    assert.equal(
      await ui.getByRole('button', { name: 'Принять поле', exact: true }).isDisabled(),
      true,
    )
    await ui.locator('#scout-visual-css').fill('p')
    await ui.getByRole('button', { name: 'Проверить CSS', exact: true }).click()
    await ui.getByText(/Для одиночного поля найдено несколько/).waitFor()

    // Late selector checks must not replace a newer selection or edited CSS.
    await page.evaluate(() => {
      window.__scoutTest.visual.defer = 'check'
    })
    await ui.getByRole('button', { name: 'Проверить CSS', exact: true }).click()
    await page.waitForFunction(() => !!window.__scoutTest.visual.release)
    await ui.locator('#scout-visual-css').fill('.price_color')
    await ui.getByRole('button', { name: 'Проверить CSS', exact: true }).click()
    await ui.getByText('Совпадений в примерах: 20', { exact: true }).waitFor()
    await page.evaluate(() => window.__scoutTest.visual.release())
    assert.equal(await ui.locator('#scout-visual-css').inputValue(), '.price_color')
    await ui.getByRole('button', { name: 'Принять поле', exact: true }).click()
    assert.equal(
      await ui.getByRole('button', { name: 'Получить предпросмотр', exact: true }).isEnabled(),
      true,
    )

    for (const theme of ['Тёмная', 'Светлая']) {
      await ui.getByRole('button', { name: 'Тема оформления', exact: true }).click()
      // Keep viewport checks independent of the dropdown's hover-close timer.
      await page.getByRole('link', { name: theme, exact: true }).press('Enter')
      for (const width of [1440, 768, 360, 320]) {
        await page.setViewportSize({ width, height: 1100 })
        assert.ok(
          await ui.evaluate((element) => element.scrollWidth <= element.clientWidth + 2),
          `Visual overflow at ${width}`,
        )
      }
    }
    await page.screenshot({ path: path.join(output, 'visual-mobile.png'), fullPage: true })
    await page.setViewportSize({ width: 1440, height: 1100 })
    await page.screenshot({ path: path.join(output, 'visual-desktop.png'), fullPage: true })
    // Changing the card context requires rechecking previously accepted fields.
    await ui.getByRole('button', { name: '1. Карточка', exact: true }).click()
    await ui.locator('#scout-visual-record').fill('article')
    await ui.getByRole('button', { name: 'Проверить CSS', exact: true }).click()
    await ui.getByRole('button', { name: 'Принять карточку', exact: true }).click()
    assert.equal(
      await ui.getByRole('button', { name: 'Получить предпросмотр', exact: true }).isDisabled(),
      true,
    )
    await ui.getByRole('button', { name: 'Изменить', exact: true }).click()
    await ui.getByRole('button', { name: 'Проверить CSS', exact: true }).click()
    await ui.getByRole('button', { name: 'Принять поле', exact: true }).click()
    assert.equal(
      await ui.getByRole('button', { name: 'Получить предпросмотр', exact: true }).isEnabled(),
      true,
    )
    // Expired snapshot invalidates the plan, but never clears accepted rules.
    await ui.getByRole('button', { name: 'Изменить', exact: true }).click()
    await page.evaluate(() => {
      window.__scoutTest.visual.error = 404
    })
    await ui.getByRole('button', { name: 'Проверить CSS', exact: true }).click()
    await ui.getByText(/Снимок истёк или недоступен/).waitFor()
    await ui.getByRole('button', { name: 'Перейти к ручным правилам', exact: true }).click()
    assert.equal(await ui.locator('#scout-url').inputValue(), 'https://books.toscrape.com/')
    assert.equal(await ui.locator('#scout-selector-0').inputValue(), '.price_color')
    await ui.getByRole('button', { name: 'Получить предпросмотр', exact: true }).click()
    await ui.getByRole('heading', { name: 'Предпросмотр готов' }).waitFor()
    const payload = await page.evaluate(() => window.__scoutTest.job.revision)
    assert.deepEqual(payload.record, { kind: 'css', selector: 'article' })
    assert.equal(payload.fields[0].scope, 'record')
    assert.equal(
      await page.evaluate(() =>
        window.__scoutTest.calls.some(
          ([method, url]) => method === 'post' && /\/(approve|start)\/$/.test(url),
        ),
      ),
      false,
    )

    // A fresh visual plan also reaches common preview directly.
    await page.goto(pathToFileURL(path.join(output, 'fixture.html')).href)
    await ui.locator('.scout-mode').nth(1).click()
    await ui.getByRole('button', { name: 'Далее: источник и правила' }).click()
    await ui.locator('#scout-visual-url').fill('https://books.toscrape.com/')
    await load.click()
    await frame.locator('p').first().click()
    await ui.getByRole('button', { name: 'Принять поле', exact: true }).click()
    await ui.getByRole('button', { name: 'Получить предпросмотр', exact: true }).click()
    await ui.getByRole('heading', { name: 'Предпросмотр готов' }).waitFor()
    assert.equal(await page.evaluate(() => window.__scoutTest.job.revision.record), null)

    // URL and mode changes invalidate a pending load, and loading failures can be retried.
    await page.goto(pathToFileURL(path.join(output, 'fixture.html')).href)
    await ui.locator('.scout-mode').nth(1).click()
    await ui.getByRole('button', { name: 'Далее: источник и правила' }).click()
    await ui.locator('#scout-visual-url').fill('https://books.toscrape.com/')
    await page.evaluate(() => {
      window.__scoutTest.visual.defer = 'get'
    })
    await load.click()
    await page.waitForFunction(() => !!window.__scoutTest.visual.release)
    await ui.locator('#scout-visual-url').fill('https://books.toscrape.com/catalogue/')
    await page.evaluate(() => window.__scoutTest.visual.release())
    assert.equal(await ui.locator('iframe').count(), 0)
    await page.evaluate(() => {
      window.__scoutTest.visual.failed = true
    })
    await load.click()
    await ui.getByText(/Не удалось загрузить страницу/).waitFor()
    await page.evaluate(() => {
      window.__scoutTest.visual.failed = false
      window.__scoutTest.visual.defer = 'get'
    })
    await load.click()
    await page.waitForFunction(() => !!window.__scoutTest.visual.release)
    await ui.getByRole('button', { name: 'Перейти к ручным правилам', exact: true }).click()
    await page.evaluate(() => window.__scoutTest.visual.release())
    assert.equal(
      await ui.locator('#scout-url').inputValue(),
      'https://books.toscrape.com/catalogue/',
    )
    assert.equal(await ui.locator('iframe').count(), 0)
    console.log(
      'Scout visual checks passed: isolated snapshot, cards/page, 20 matches, manual CSS, empty/multiple, stale checks/loads, failed/expired snapshot, preserved rules, shared preview, themes and 320–1440px.',
    )
  } catch (error) {
    await page.screenshot({ path: path.join(output, 'visual-failure.png'), fullPage: true })
    console.error(await page.locator('body').innerText())
    throw error
  } finally {
    await page.close()
  }
}
