const assert = require('node:assert/strict')
const path = require('node:path')
const { pathToFileURL } = require('node:url')

module.exports = async (browser, output) => {
  const page = await browser.newPage({ viewport: { width: 1440, height: 1100 } })
  try {
    await page.goto(pathToFileURL(path.join(output, 'fixture.html')).href + '#/ux-004')
    const ui = page.locator('.scout-workflow')
    await ui.getByText(/Прототип UX-004/).waitFor()
    await ui.getByRole('button', { name: 'Выбрать на странице', exact: true }).click()
    await ui.locator('#scout-visual-url').fill('https://books.toscrape.com/')
    await ui.getByRole('button', { name: 'Загрузить страницу', exact: true }).click()
    const frame = page.frameLocator('.scout-snapshot-frame iframe')
    const preview = ui.getByRole('button', { name: /^К предпросмотру/ })
    const refresh = ui.getByRole('button', { name: 'Обновить снимок', exact: true })
    await frame.locator('article').first().waitFor()
    // Opening a second branch while the first response is delayed must remain independent.
    const firstBranch = ui.locator('[role=treeitem][data-node=n1]')
    const secondBranch = ui.locator('[role=treeitem][data-node=n4]')
    await firstBranch.waitFor()
    await page.evaluate(() => {
      window.__scoutTest.visual.defer = 'tree'
    })
    await firstBranch.locator('button').click()
    await page.waitForFunction(() => !!window.__scoutTest.visual.release)
    await secondBranch.locator('button').click()
    await page.waitForFunction(
      () => document.querySelector('[data-node=n4]')?.getAttribute('aria-expanded') === 'true',
    )
    await page.evaluate(() => window.__scoutTest.visual.release())
    await ui.locator('[role=treeitem][data-node=n2]').waitFor()
    assert.equal(await firstBranch.getAttribute('aria-expanded'), 'true')
    assert.equal(
      await secondBranch.getAttribute('aria-expanded'),
      'true',
      'Late first branch response collapsed the second branch',
    )

    // A user can also collapse a branch before its children arrive.
    const thirdBranch = ui.locator('[role=treeitem][data-node=n7]')
    await page.evaluate(() => {
      window.__scoutTest.visual.defer = 'tree'
    })
    await thirdBranch.locator('button').click()
    await page.waitForFunction(() => !!window.__scoutTest.visual.release)
    await thirdBranch.locator('button').click()
    await page.evaluate(() => window.__scoutTest.visual.release())
    assert.equal(
      await thirdBranch.getAttribute('aria-expanded'),
      'false',
      'Late children reopened a collapsed branch',
    )
    const checks = () =>
      page.evaluate(
        () =>
          window.__scoutTest.calls.filter(
            ([method, url]) => method === 'post' && url.endsWith('/check/'),
          ).length,
      )
    const before = await checks()
    await frame.locator('p').first().hover()
    await ui.locator('rect[data-role="hover"]').waitFor({ state: 'attached' })
    assert.equal(await checks(), before)
    await ui.locator('.scout-visual-record-settings > summary').click()
    await ui.locator('#scout-visual-cards').check()
    await frame.locator('p').first().click()
    await ui.locator('.scout-visual-navigator > summary').click()
    await ui.getByRole('button', { name: 'Родитель', exact: true }).click()
    await ui
      .locator('.scout-prototype-selected-label')
      .filter({ hasText: 'article.product_pod' })
      .waitFor()
    assert.equal(await checks(), before)
    await ui.getByRole('button', { name: 'Проверить правило', exact: true }).click()
    await ui.getByRole('button', { name: 'Принять карточку', exact: true }).click()
    await ui.locator('#scout-dom-search').fill('price_color')
    await ui.getByRole('button', { name: 'Найти', exact: true }).click()
    await ui.getByText('Найдено: 20. В этой порции: 20.', { exact: true }).waitFor()
    await ui.locator('.scout-prototype-search-results .scout-prototype-node').last().click()
    await ui
      .locator('.scout-prototype-selected-label')
      .filter({ hasText: 'p.price_color' })
      .waitFor()
    await ui.locator('[role=treeitem][data-node=n60]').waitFor()
    await ui.locator('#scout-visual-name').fill('price')
    await ui.getByRole('button', { name: 'Проверить правило', exact: true }).click()
    await ui.getByText('Совпадений: 20', { exact: true }).waitFor()
    const body = await page.evaluate(
      () =>
        window.__scoutTest.calls
          .filter(([method, url]) => method === 'post' && url.endsWith('/check/'))
          .at(-1)[2],
    )
    assert.match(body.tree_version, /^dom-v1:/)
    await ui.getByRole('button', { name: 'Принять поле', exact: true }).click()
    await ui
      .locator('.scout-visual-fields')
      .getByRole('button', { name: 'Изменить', exact: true })
      .click()
    await ui.locator('#scout-visual-css').fill('p.price_color')
    await ui.getByRole('button', { name: 'Проверить правило', exact: true }).click()
    await ui.getByRole('button', { name: 'Сохранить изменения', exact: true }).click()
    assert.equal(await ui.locator('.scout-visual-fields li').count(), 1)
    assert.equal(await ui.locator('.scout-visual-fields code').textContent(), 'p.price_color')
    const savedPrice = ui.locator('.scout-visual-fields li').filter({ hasText: 'price' })
    const cancel = ui.getByRole('button', { name: 'Отменить выбор', exact: true })
    await savedPrice.getByRole('button', { name: 'Изменить', exact: true }).click()
    assert.equal(await cancel.isEnabled(), true)
    await ui.locator('#scout-visual-name').fill('discarded')
    await ui.locator('#scout-visual-css').fill('h3')
    await page.evaluate(() => {
      window.__scoutTest.visual.defer = 'check'
    })
    await ui.getByRole('button', { name: 'Проверить правило', exact: true }).click()
    await page.waitForFunction(() => !!window.__scoutTest.visual.release)
    await cancel.click()
    await page.evaluate(() => window.__scoutTest.visual.release())
    assert.equal(await ui.locator('.scout-visual-check').count(), 0)
    assert.equal(await ui.locator('#scout-visual-css').inputValue(), 'p.price_color')
    assert.equal(await cancel.count(), 0)
    assert.equal(await savedPrice.locator('code').textContent(), 'p.price_color')
    assert.equal(await preview.isEnabled(), true)
    await savedPrice.getByRole('button', { name: 'Изменить', exact: true }).click()
    assert.equal(await ui.locator('#scout-visual-name').inputValue(), 'price')
    assert.equal(await ui.locator('#scout-visual-css').inputValue(), 'p.price_color')
    await cancel.click()
    await ui.getByRole('button', { name: 'Правило CSS', exact: true }).click()
    await ui.locator('#scout-visual-name').fill('title')
    await ui.locator('#scout-visual-css').fill('h3')
    await ui.getByRole('button', { name: 'Проверить правило', exact: true }).click()
    await ui.getByRole('button', { name: 'Принять поле', exact: true }).click()
    assert.equal(await ui.locator('.scout-visual-fields li').count(), 2)
    await ui
      .locator('.scout-visual-fields li')
      .filter({ hasText: 'title' })
      .getByRole('button', { name: 'Удалить', exact: true })
      .click()
    const separator = ui.getByRole('separator').first()
    await separator.focus()
    const width = Number(await separator.getAttribute('aria-valuenow'))
    await separator.press('ArrowRight')
    assert.equal(Number(await separator.getAttribute('aria-valuenow')), width + 10)
    await separator.hover({ position: { x: 5, y: 200 } })
    const handle = await separator.boundingBox()
    await page.mouse.down()
    await page.mouse.move(handle.x + 40, handle.y + 200)
    await page.mouse.up()
    assert(Number(await separator.getAttribute('aria-valuenow')) > width + 10) +
      +(await ui.locator('#scout-dom-search').fill('does-not-exist'))
    await ui.getByRole('button', { name: 'Найти', exact: true }).click()
    await ui.getByText('Найдено: 0. В этой порции: 0.', { exact: true }).waitFor()
    await frame.locator('p').first().click()
    await ui.getByRole('button', { name: 'Отменить выбор', exact: true }).click()
    assert.equal(await ui.locator('.scout-visual-fields li').count(), 1)
    assert.equal(await preview.isEnabled(), true)
    await ui.locator('[role=treeitem]').first().focus()
    await page.keyboard.press('ArrowDown')
    await page.keyboard.press('Enter')
    await ui.locator('.scout-prototype-selected-label').waitFor()
    for (const width of [1440, 1024, 768, 390, 320]) {
      await page.setViewportSize({ width, height: 1100 })
      const tabs = ui.locator('.scout-prototype-tabs')
      if (width <= 768) await tabs.waitFor({ state: 'visible' })
      if (await tabs.isVisible()) {
        for (const tab of ['Структура', 'Поля', 'Страница'])
          await ui
            .locator('.scout-prototype-tabs')
            .getByRole('tab', { name: new RegExp('^' + tab) })
            .click()
      }
      assert.ok(
        await ui.evaluate((element) => element.scrollWidth <= element.clientWidth + 2),
        `UX-004 overflow at ${width}`,
      )
    }
    await page.screenshot({ path: path.join(output, 'ux004-mobile.png'), fullPage: true })
    await page.setViewportSize({ width: 1440, height: 1100 })
    await page.screenshot({ path: path.join(output, 'ux004-desktop.png'), fullPage: true })
    // A late node response cannot restore a cancelled draft selection.
    await page.evaluate(() => {
      window.__scoutTest.visual.defer = 'node'
    })
    await frame.locator('p').first().click()
    await page.waitForFunction(() => !!window.__scoutTest.visual.release)
    await ui.getByRole('button', { name: 'Отменить выбор', exact: true }).click()
    await page.evaluate(() => window.__scoutTest.visual.release())
    assert.equal(await ui.locator('.scout-prototype-selected-label').count(), 0)
    await page.evaluate(() => {
      window.__scoutTest.visual.treeError = 409
    })
    await frame.locator('p').first().click()
    await ui.getByRole('alert').filter({ hasText: 'Версия снимка изменилась' }).waitFor()
    assert.equal(await preview.count(), 0)
    // A late branch expansion cannot carry old node IDs into a new snapshot.
    await page.evaluate(() => {
      window.__scoutTest.visual.treeError = 0
    })
    await ui.getByRole('button', { name: 'Загрузить страницу заново', exact: true }).click()
    await ui.locator('[role=treeitem]').first().waitFor()
    await page.evaluate(() => {
      window.__scoutTest.visual.defer = 'tree'
    })
    await ui.locator('[role=treeitem]').first().locator('button').click()
    await page.waitForFunction(() => !!window.__scoutTest.visual.release)
    await ui.locator('#scout-visual-url').fill('https://books.toscrape.com/new-snapshot')
    await page.evaluate(() => window.__scoutTest.visual.release())
    assert.equal(await ui.locator('[role=treeitem]').count(), 0)
    assert.equal(await ui.locator('iframe').count(), 0)
    await ui.getByRole('button', { name: 'Загрузить страницу', exact: true }).click()
    await frame.locator('p').first().waitFor()
    // Saved rules survive reload, but each needs explicit verification on the new snapshot.
    async function reviewCard() {
      await ui.getByRole('button', { name: 'Перепроверить карточку', exact: true }).click()
      await ui.getByRole('button', { name: 'Проверить правило', exact: true }).click()
      await ui.getByRole('button', { name: 'Принять карточку', exact: true }).click()
    }
    async function reviewField(name) {
      await ui.getByRole('button', { name: 'Перепроверить следующее поле', exact: true }).click()
      assert.equal(await ui.locator('#scout-visual-name').inputValue(), name)
      await ui.getByRole('button', { name: 'Проверить правило', exact: true }).click()
      await ui.getByRole('button', { name: 'Сохранить изменения', exact: true }).click()
    }
    await reviewCard()
    await reviewField('price')
    await ui.getByRole('button', { name: 'Правило CSS', exact: true }).click()
    await ui.locator('#scout-visual-name').fill('title')
    await ui.locator('#scout-visual-css').fill('h3')
    await ui.getByRole('button', { name: 'Проверить правило', exact: true }).click()
    await ui.getByRole('button', { name: 'Принять поле', exact: true }).click()
    assert.equal(await ui.locator('.scout-field-verification[data-verified=true]').count(), 2)
    await refresh.click()
    await frame.locator('p').first().waitFor()
    assert.equal(await preview.count(), 0)
    await ui
      .locator('#scout-preview-requirements')
      .filter({ hasText: 'Сначала проверьте и примите правило карточки' })
      .waitFor()
    await reviewCard()
    await ui.locator('#scout-preview-requirements').filter({ hasText: 'price, title' }).waitFor()
    assert.equal(await ui.locator('.scout-field-verification[data-verified=false]').count(), 2)
    for (const width of [390, 768, 1440]) {
      await page.setViewportSize({ width, height: 1100 })
      await page.waitForFunction(() => {
        const editor = document.querySelector('.scout-visual-editor')
        return editor && editor.scrollWidth <= editor.clientWidth + 2
      })
      assert(
        await ui
          .getByRole('button', { name: 'Перепроверить следующее поле', exact: true })
          .isVisible(),
      )
    }
    await ui.locator('.scout-visual-fields').scrollIntoViewIfNeeded()
    await page.screenshot({ path: path.join(output, 'preview-recheck-required.png') })

    await reviewField('price')
    assert.equal(await ui.locator('.scout-field-verification[data-verified=false]').count(), 1)
    assert.equal(
      await ui.locator('#scout-preview-requirements').textContent(),
      'Нужно перепроверить поля на текущем снимке: title. Всего осталось: 1.',
    )
    await reviewField('title')
    await ui
      .locator('#scout-preview-requirements')
      .filter({ hasText: 'Все поля проверены: 2' })
      .waitFor()
    assert(await preview.isEnabled())
    await ui.locator('#scout-visual-page').check()
    await frame.locator('p').first().click()
    await ui.locator('#scout-visual-name').fill('page_price')
    await ui.getByRole('button', { name: 'Проверить правило', exact: true }).click()
    await ui.locator('.scout-visual-check').waitFor()
    const single = await page.evaluate(
      () =>
        window.__scoutTest.calls
          .filter((c) => c[0] === 'post' && c[1].endsWith('/check/'))
          .at(-1)[2],
    )
    assert.equal(single.record, null)
    assert.equal(single.field.scope, 'page') +
      console.log(
        'UX-004 prototype passed: hover/click/check separation, API tree/search, offscreen selection, cancel, keyboard, tabs, version conflict, late response and 320–1440px.',
      )
  } catch (error) {
    await page.screenshot({ path: path.join(output, 'ux004-failure.png'), fullPage: true })
    console.error(await page.locator('body').innerText())
    throw error
  } finally {
    await page.close()
  }
}
