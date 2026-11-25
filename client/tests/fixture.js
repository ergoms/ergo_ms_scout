// Isolated browser fixture: real Scout UI, core controls and router, simulated API only.
import { createApp, h, reactive } from 'vue'
import { createPinia } from 'pinia'
import { createRouter, createWebHashHistory, RouterView } from 'vue-router'
import '@/scss/styles.scss'
import '@/js/utils/logger.js'
import NotificationProvider from '@/components/NotificationProvider.vue'
import { ensureBootLocales, i18n } from '@/i18n/index.js'
import { initTheme, applyThemeModePreference } from '@/js/theme-manager.js'
import { apiClient } from '@/js/api/manager'
import ScoutComparisonPage from '../components/ScoutComparisonPage.vue'
import '../styles/scout-comparison.scss'
import { installComparisonFixture } from './comparison-fixture.js'
import ScoutRetentionPage from '../components/ScoutRetentionPage.vue'
import '../styles/scout-retention.scss'
import { installRetentionFixture } from './retention-fixture.js'
import ScoutSchedulePanel from '../components/ScoutSchedulePanel.vue'
import { installScheduleFixture } from './schedule-fixture.js'
import ScoutPage from '../components/ScoutPage.vue'
import ScoutUX004 from '../prototypes/ScoutUX004.vue'
import locales from '../js/locales.js'
import '../styles/scout-workflow.scss'
import '../prototypes/inspector.scss'
import { installVisualFixture } from './visual-fixture.js'

const jobId = '11111111-1111-4111-8111-111111111111'
const runId = '22222222-2222-4222-8222-222222222222'
const fixture = {
  calls: [],
  systemOpened: 0,
  failAction: '',
  networkDown: false,
  deferNextRecords: false,
  releaseRecords: null,
  deferPreview: false,
  pauseStatus: 'pausing',
  run: null,
  job: null,
  setStatus(status) {
    this.run.status = status
  },
}
const clone = (value) => JSON.parse(JSON.stringify(value))
const result = (data) => ({ success: true, data: clone(data) })
function imageCells(index, preview) {
  const cells = {}
  for (const field of fixture.job?.revision?.fields || []) {
    if (field.value_source !== 'image') continue
    const image = {
      source_url: 'https://books.toscrape.com/cover.png',
      status: preview ? 'candidate' : ['saved', 'failed', 'limit'][index % 3],
    }
    if (image.status === 'saved')
      Object.assign(image, {
        public_id: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
        content_type: 'image/png',
        byte_size: 69,
      })
    cells[field.name] = { images: [image] }
  }
  return cells
}
const rows = (length = 25, preview = false) =>
  Array.from({ length }, (_, index) => ({
    public_id: `record-${index + 1}`,
    values: {
      title: { raw: `Событие ${index + 1}`, normalized: `Событие ${index + 1}` },
      count: { raw: 0, normalized: 0 },
      ...imageCells(index, preview),
    },
    evidence: [
      {
        field_path: 'title',
        source_url: 'https://events.example/list',
        locator: '.title',
        raw_value: `Событие ${index + 1}`,
      },
    ],
  }))

apiClient.get = async (url, params = {}) => {
  fixture.calls.push(['get', url, clone(params)])
  if (fixture.networkDown) throw new Error('Fixture network interruption')
  if (url.endsWith('/records/')) {
    if (fixture.deferNextRecords) {
      fixture.deferNextRecords = false
      await new Promise((resolve) => {
        fixture.releaseRecords = () => {
          fixture.releaseRecords = null
          resolve()
        }
      })
    }
    const all = rows(fixture.run.record_count)
    if (params.order === 'recent') all.reverse()
    const first = params.cursor ? all.findIndex((row) => row.public_id === params.cursor) + 1 : 0
    const records = all.slice(first, first + (params.limit ?? all.length))
    return result({
      records,
      next_cursor: first + records.length < all.length ? records.at(-1).public_id : null,
    })
  }
  if (url.startsWith('scout/jobs/')) return result(fixture.job)
  if (url.startsWith('scout/runs/')) {
    if (!params.summary) return result(fixture.run)
    const {
      journal: _journal,
      events: _events,
      tasks: _tasks,
      preview: _preview,
      ...summary
    } = fixture.run
    return result({ ...summary, progress: { fetched: 1, extracted: 0, errors: 0 } })
  }
  return result({})
}
apiClient.patch = async (url, body) => {
  fixture.calls.push(['patch', url, clone(body)])
  const publicId = url.split('/').at(-2)
  const limits = fixture.job?.public_id === publicId ? fixture.job.revision.limits : undefined
  fixture.job = {
    public_id: publicId,
    title: body.title,
    revision: {
      ...clone(body),
      limits,
      public_id: 'fixture-revision',
      plan_hash: 'fixture-plan',
      approved: false,
    },
  }
  return result(fixture.job)
}
apiClient.post = async (url, body) => {
  fixture.calls.push(['post', url])
  if (url === 'scout/jobs/') return apiClient.patch(`scout/jobs/${jobId}/`, body)
  if (url.endsWith('/explore/')) {
    fixture.run = {
      public_id: runId,
      job_public_id: fixture.job.public_id,
      plan_hash: fixture.job.revision.plan_hash,
      approved: false,
      status: fixture.deferPreview ? 'exploring' : 'awaiting_confirmation',
      record_count: 0,
      snapshot_count: 1,
      preview: rows(3, true),
      tasks: [],
      events: [],
      journal: [],
      crawl: fixture.job.revision.crawl,
    }
    return result(fixture.run)
  }
  const action = url.split('/').filter(Boolean).at(-1)
  if (fixture.failAction === action) {
    fixture.failAction = ''
    return { success: false, message: 'Fixture rejection' }
  }
  if (action === 'approve') {
    fixture.run.approved = true
    fixture.job.revision.approved = true
  }
  if (action === 'start') {
    fixture.run.status = 'running'
    fixture.run.record_count = 25
  }
  if (action === 'pause') fixture.run.status = fixture.pauseStatus
  if (action === 'resume') fixture.run.status = 'running'
  if (action === 'cancel') fixture.run.status = 'cancelled'
  return result(fixture.run || {})
}
installVisualFixture(apiClient, fixture, result)
// Requests are mocked. Exercise the same URL resolution as the real API client.
apiClient.client.defaults.baseURL = new URLSearchParams(location.search).has('httpApi')
  ? 'http://api.example.test/api/'
  : 'https://api.example.test/api/'
fixture.suggestionError = false
const productPost = apiClient.post
const productGet = apiClient.get
apiClient.get = async (url, params = {}) => {
  if (url === 'scout/proxy-profiles/')
    return result({
      profiles: [
        {
          public_id: 'proxy-1',
          revision_public_id: 'proxy-v1',
          ip: '192.0.2.10',
          port: 8443,
          auth_type: 'none',
          credentials_configured: false,
        },
      ],
    })
  return productGet(url, params)
}
apiClient.post = async (url, body) => {
  if (url === 'scout/plan-suggestions/') {
    fixture.calls.push(['post', url, clone(body)])
    if (fixture.suggestionError)
      return {
        success: false,
        status: 503,
        data: { code: 'model_unavailable', detail: 'Модель временно недоступна' },
      }
    if (fixture.deferSuggestion)
      return new Promise((resolve) => {
        fixture.releaseSuggestion = () =>
          resolve(
            result({
              draft: {
                seed_urls: ['https://late.example'],
                fields: [
                  { name: 'late', kind: 'css', selector: '.late', scope: 'page', many: false },
                ],
                crawl: { mode: 'pages' },
              },
            }),
          )
      })
    return result({
      draft: {
        seed_urls: ['https://books.toscrape.com/'],
        record: { kind: 'css', selector: 'article' },
        fields: [
          { name: 'title', kind: 'css', selector: 'h3', scope: 'record', many: false },
          {
            name: 'price',
            kind: 'css',
            selector: '.price_color',
            scope: 'record',
            many: false,
            conversion: { type: 'money', currency: 'RUB', decimal_separator: ',' },
          },
        ],
        crawl: { mode: 'pages' },
      },
    })
  }
  if (url.startsWith('scout/proxy-profiles/')) {
    fixture.calls.push(['post', url, clone(body)])
    if (url === 'scout/proxy-profiles/') {
      fixture.profilesCreated = (fixture.profilesCreated || 0) + 1
      return result({
        public_id: 'proxy-' + (fixture.profilesCreated + 1),
        revision_public_id: 'proxy-v' + (fixture.profilesCreated + 1),
        ip: body.ip,
        port: body.port,
        auth_type: body.auth_type,
        credentials_configured: body.auth_type === 'basic',
      })
    }
    return result({ status: 'ready' })
  }
  return productPost(url, body)
}
const scheduleContext = reactive({
  jobId,
  plan: { public_id: 'revision-one', plan_hash: 'hash-one', approved: true, revision_number: 1 },
  planDirty: false,
})
installScheduleFixture(apiClient, fixture, scheduleContext)
installRetentionFixture(apiClient, fixture)
installComparisonFixture(apiClient, fixture)
window.__scoutTest = fixture
fixture.setTheme = applyThemeModePreference
window.addEventListener('ergo:open-user-settings', () => {
  fixture.systemOpened += 1
})
await ensureBootLocales()
i18n.global.locale.value = 'ru'
Object.entries(locales).forEach(([locale, messages]) =>
  i18n.global.mergeLocaleMessage(locale, messages),
)
initTheme()
const router = createRouter({
  history: createWebHashHistory(),
  routes: [
    { path: '/scout/compare', component: ScoutComparisonPage },
    { path: '/scout/storage', component: ScoutRetentionPage },
    {
      path: '/schedule-test',
      component: {
        render: () =>
          h('main', { class: 'scout-workflow' }, [h(ScoutSchedulePanel, scheduleContext)]),
      },
    },
    { path: '/ux-004', component: ScoutUX004 },
    { path: '/product', component: ScoutPage },
    {
      path: '/:pathMatch(.*)*',
      component: { render: () => h(ScoutPage, { inspectorEnabled: false }) },
    },
  ],
})
const app = createApp({ render: () => h('div', [h(RouterView), h(NotificationProvider)]) })
app.use(createPinia()).use(i18n).use(router)
await router.isReady()
app.mount('#app')
