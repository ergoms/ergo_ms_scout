// Isolated browser fixture: real Scout UI, core controls and router, simulated API only.
import { createApp, h } from 'vue'
import { createPinia } from 'pinia'
import { createRouter, createWebHashHistory, RouterView } from 'vue-router'
import '@/scss/styles.scss'
import '@/js/utils/logger.js'
import NotificationProvider from '@/components/NotificationProvider.vue'
import { ensureBootLocales, i18n } from '@/i18n/index.js'
import { initTheme } from '@/js/theme-manager.js'
import { apiClient } from '@/js/api/manager'
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
const rows = (length = 25) =>
  Array.from({ length }, (_, index) => ({
    public_id: `record-${index + 1}`,
    values: {
      title: { raw: `Событие ${index + 1}`, normalized: `Событие ${index + 1}` },
      count: { raw: 0, normalized: 0 },
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
    const { journal: _journal, events: _events, tasks: _tasks, preview: _preview, ...summary } = fixture.run
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
    revision: { ...clone(body), limits, plan_hash: 'fixture-plan', approved: false },
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
      preview: rows().slice(0, 3),
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
  if (action === 'approve') fixture.run.approved = true
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
window.__scoutTest = fixture
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
    { path: '/ux-004', component: ScoutUX004 },
    { path: '/:pathMatch(.*)*', component: ScoutPage },
  ],
})
const app = createApp({ render: () => h('div', [h(RouterView), h(NotificationProvider)]) })
app.use(createPinia()).use(i18n).use(router)
await router.isReady()
app.mount('#app')
