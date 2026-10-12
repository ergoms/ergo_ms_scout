<script setup>
import { computed, onUnmounted, ref, watch } from 'vue'

import DataTable from '@/components/DataTable.vue'
import FormCard from '@/components/FormCard.vue'
import FormField from '@/components/FormField.vue'
import SelectBox from '@/components/SelectBox.vue'
import { useRouteQueryState } from '@/composables/useRouteQueryState.js'
import { useAppI18n } from '@/i18n/useAppI18n.js'
import { apiClient } from '@/js/api/manager'
import { handleApiError, showError } from '@/js/utils/toast.js'
import { downloadMediaFromResult } from '@/js/utils/mediaDownload.js'

import { scoutEndpoints } from '../js/endpoints.js'

const PREVIEW_LIMIT = 20
const STEPS = ['setup', 'preview', 'collect', 'result']
const ACTIVE = ['exploring', 'running', 'pausing', 'cancelling']
const TERMINAL = ['cancelled', 'completed', 'completed_with_errors', 'failed']
const EXPORTABLE = ['completed', 'completed_with_errors']

const { t } = useAppI18n()
const { state: query, patchState } = useRouteQueryState({
  job: { default: '' },
  run: { default: '' },
})

const title = ref('')
const url = ref('')
const recordMode = ref('page')
const recordKind = ref('css')
const recordSelector = ref('')
const fields = ref([blankField()])
const run = ref(null)
const records = ref([])
const loading = ref(false)
const savedStamp = ref('')
const urlError = ref('')
const cardError = ref('')
const fieldErrors = ref([''])
const exportKind = ref('json')
const returnToSetup = ref(false)
let pollTimer = 0
let loadToken = 0
let shownRun = ''
let editorGeneration = 0

const kindOptions = computed(() => [
  { id: 'css', name: t('scout.fields.css') },
  { id: 'xpath', name: t('scout.fields.xpath') },
])
const scopeOptions = computed(() => [
  { id: 'page', name: t('scout.scopePage') },
  { id: 'record', name: t('scout.scopeRecord') },
])
const exportOptions = computed(() => [
  { id: 'json', name: t('scout.flow.json') },
  { id: 'csv', name: t('scout.flow.csv') },
])

const stage = computed(() => {
  const status = run.value?.status
  if (!status) return 'setup'
  if (returnToSetup.value) return 'preview'
  if (status === 'exploring' || status === 'awaiting_confirmation') return 'preview'
  if (['running', 'pausing', 'paused', 'cancelling'].includes(status)) return 'collect'
  return 'result'
})

const confirmedNotStarted = computed(() => (
  run.value?.status === 'awaiting_confirmation' && run.value?.approved && !returnToSetup.value
))

const sourceRows = computed(() => {
  if (stage.value === 'result' || stage.value === 'collect') {
    if (records.value.length) return records.value
  }
  return Array.isArray(run.value?.preview) ? run.value.preview : []
})

const shownRows = computed(() => {
  if (stage.value === 'preview') return sourceRows.value.slice(0, PREVIEW_LIMIT)
  if (stage.value === 'collect') return sourceRows.value.slice(-PREVIEW_LIMIT)
  return sourceRows.value
})
const tableRows = computed(() => shownRows.value.map((row, index) => flattenValues(row.values, row.public_id || index)))
const columns = computed(() => {
  const keys = []
  tableRows.value.forEach((row) => {
    Object.keys(row).forEach((key) => {
      if (key !== 'id' && !keys.includes(key)) keys.push(key)
    })
  })
  return keys.map((key) => ({ key, label: key }))
})

const gapCount = computed(() => tableRows.value.reduce((sum, row) => (
  sum + Object.entries(row).filter(([key, value]) => key !== 'id' && value === t('scout.flow.notFound')).length
), 0))

const pageProgress = computed(() => {
  const tasks = Array.isArray(run.value?.tasks) ? run.value.tasks : []
  const fetches = tasks.filter((task) => task.kind === 'fetch')
  const extracts = tasks.filter((task) => task.kind === 'extract')
  return {
    fetched: fetches.filter((task) => task.status === 'succeeded').length,
    fetchTotal: fetches.length,
    extracted: extracts.filter((task) => task.status === 'succeeded').length,
    extractTotal: extracts.length,
    records: run.value?.record_count || 0,
    errors: tasks.filter((task) => task.status === 'failed').length,
  }
})

const journal = computed(() => (Array.isArray(run.value?.journal) ? run.value.journal : []))
const events = computed(() => (Array.isArray(run.value?.events) ? run.value.events : []))
const evidenceLines = computed(() => shownRows.value.flatMap((row, index) => (
  (row.evidence || []).map((item) => {
    const value = item.normalized_value || item.raw_value || t('scout.flow.notFound')
    const place = [item.source_url || row.source_url, item.locator].filter(Boolean).join(' · ')
    return place
      ? `${index + 1}. ${item.field_path}: ${value} — ${place}`
      : `${index + 1}. ${item.field_path}: ${value}`
  })
)))

watch(() => query.value.job, (publicId) => {
  if (publicId) loadJob(publicId)
}, { immediate: true })

watch(() => query.value.run, (publicId) => {
  if (publicId) loadRun(publicId)
  else window.clearTimeout(pollTimer)
}, { immediate: true })

onUnmounted(() => window.clearTimeout(pollTimer))

function blankField() {
  return {
    name: 'title',
    kind: 'css',
    selector: '',
    scope: 'page',
    many: false,
  }
}

function payload() {
  const selector = recordSelector.value.trim()
  const cards = recordMode.value === 'cards'
  return {
    title: title.value,
    seed_urls: [url.value],
    record: cards && selector ? { kind: recordKind.value, selector } : null,
    fields: fields.value.map((field) => ({
      name: field.name.trim(),
      kind: field.kind,
      selector: field.selector.trim(),
      scope: cards && field.scope === 'record' ? 'record' : 'page',
      many: field.many,
    })),
  }
}

function stamp() {
  return JSON.stringify(payload())
}

function applyJob(job) {
  title.value = job?.title || ''
  const revision = job?.revision
  url.value = Array.isArray(revision?.seed_urls) ? (revision.seed_urls[0] || '') : ''
  const savedRecord = revision?.record
  recordMode.value = savedRecord?.selector ? 'cards' : 'page'
  recordKind.value = savedRecord?.kind === 'xpath' ? 'xpath' : 'css'
  recordSelector.value = savedRecord?.selector || ''
  const savedFields = Array.isArray(revision?.fields) ? revision.fields : []
  if (!savedFields.length) return
  fields.value = savedFields.map((field) => ({
    name: field.name || '',
    kind: field.kind === 'xpath' ? 'xpath' : 'css',
    selector: field.selector || '',
    scope: field.scope === 'record' ? 'record' : 'page',
    many: Boolean(field.many),
  }))
  fieldErrors.value = fields.value.map(() => '')
}

function addField() {
  fields.value.push({
    name: '',
    kind: 'css',
    selector: '',
    scope: recordMode.value === 'cards' ? 'record' : 'page',
    many: false,
  })
  fieldErrors.value.push('')
}

function removeField(index) {
  fields.value.splice(index, 1)
  fieldErrors.value.splice(index, 1)
}

function formValid() {
  urlError.value = url.value.trim().startsWith('http') ? '' : t('scout.flow.urlRequired')
  cardError.value = recordMode.value === 'cards' && !recordSelector.value.trim()
    ? t('scout.flow.cardRequired')
    : ''
  fieldErrors.value = fields.value.map((field) => (
    field.name.trim() && field.selector.trim() ? '' : t('scout.flow.fieldRequired')
  ))
  return !urlError.value && !cardError.value && fieldErrors.value.every((item) => !item)
}

function stepMark(id) {
  const current = STEPS.indexOf(stage.value)
  const index = STEPS.indexOf(id)
  if (index < current) return 'done'
  if (index === current) return 'current'
  return 'future'
}

function cellText(cell) {
  const text = String(cell?.normalized || cell?.raw || '').trim()
  return text || t('scout.flow.notFound')
}

function flattenValues(values, id) {
  const row = { id }
  Object.entries(values || {}).forEach(([key, cell]) => {
    row[key] = cellText(cell)
  })
  return row
}

function clearRunView() {
  window.clearTimeout(pollTimer)
  run.value = null
  records.value = []
  shownRun = ''
  returnToSetup.value = false
  patchState({ run: '' })
}

async function loadJob(publicId) {
  try {
    const response = await apiClient.get(scoutEndpoints.job(publicId))
    if (!response.success || query.value.job !== publicId) return
    applyJob(response.data)
    savedStamp.value = stamp()
  } catch (error) {
    logError('SCOUT: не удалось прочитать задание', error)
  }
}

async function checkPage() {
  if (loading.value || !formValid()) return
  const generation = ++editorGeneration
  loading.value = true
  try {
    const body = payload()
    const existing = query.value.job
    const saved = existing
      ? await apiClient.patch(scoutEndpoints.job(existing), body)
      : await apiClient.post(scoutEndpoints.jobs, body)
    if (generation !== editorGeneration) return
    if (!saved.success) {
      showError(saved.message || t('scout.flow.checkFailed'))
      return
    }
    applyJob(saved.data)
    savedStamp.value = stamp()
    patchState({ job: saved.data.public_id, run: '' })
    const explored = await apiClient.post(scoutEndpoints.explore(saved.data.public_id), {})
    if (generation !== editorGeneration) return
    if (!explored.success) {
      showError(explored.message || t('scout.flow.checkFailed'))
      return
    }
    records.value = []
    shownRun = explored.data.public_id
    run.value = explored.data
    patchState({ run: explored.data.public_id })
    schedulePoll()
  } catch (error) {
    if (generation !== editorGeneration) return
    logError('SCOUT: не удалось проверить страницу', error)
    await handleApiError(error, t('scout.flow.checkPage'))
  } finally {
    if (generation === editorGeneration) loading.value = false
  }
}

function newJob() {
  editorGeneration += 1
  title.value = ''
  url.value = ''
  recordMode.value = 'page'
  recordKind.value = 'css'
  recordSelector.value = ''
  fields.value = [blankField()]
  fieldErrors.value = ['']
  urlError.value = ''
  cardError.value = ''
  savedStamp.value = ''
  clearRunView()
  patchState({ job: '', run: '' })
}

async function confirmAndStart() {
  if (!query.value.run || loading.value || stamp() !== savedStamp.value) return
  const runId = query.value.run
  loading.value = true
  try {
    if (!run.value?.approved) {
      const approved = await apiClient.post(scoutEndpoints.approve(runId), {})
      if (!approved.success) {
        showError(approved.message || t('scout.flow.confirmStart'))
        return
      }
      if (query.value.run !== runId) return
      run.value = approved.data
    }
    const started = await apiClient.post(scoutEndpoints.start(runId), {})
    if (!started.success) {
      showError(started.message || t('scout.flow.startHeld'))
      await loadRun(runId, { quiet: true })
      return
    }
    if (query.value.run !== runId) return
    run.value = started.data
    if (ACTIVE.includes(started.data.status)) schedulePoll()
  } catch (error) {
    logError('SCOUT: не удалось начать сбор', error)
    await loadRun(runId, { quiet: true })
  } finally {
    loading.value = false
  }
}

async function act(endpoint, label) {
  if (!query.value.run || loading.value) return
  const runId = query.value.run
  loading.value = true
  try {
    const response = await apiClient.post(endpoint(runId), {})
    if (!response.success) {
      returnToSetup.value = false
      showError(response.message || label)
      return
    }
    if (query.value.run !== runId) return
    if (finishReturn(response.data)) return
    run.value = response.data
    if (ACTIVE.includes(response.data.status)) schedulePoll()
    if (response.data.record_count) await loadRecords(runId, loadToken)
    else records.value = []
  } catch (error) {
    returnToSetup.value = false
    logError('SCOUT: действие со сбором не выполнено', error)
    await handleApiError(error, label)
  } finally {
    loading.value = false
  }
}

function finishReturn(data) {
  if (!returnToSetup.value || !TERMINAL.includes(data.status)) return false
  clearRunView()
  return true
}

async function changeSettings() {
  const status = run.value?.status
  if (!status || TERMINAL.includes(status)) {
    clearRunView()
    return
  }
  returnToSetup.value = true
  await act(scoutEndpoints.cancel, t('scout.cancel'))
}

async function exportFile() {
  if (!query.value.run || !EXPORTABLE.includes(run.value?.status)) return
  try {
    const response = await apiClient.post(scoutEndpoints.exportRun(query.value.run), { kind: exportKind.value })
    if (!response.success) {
      showError(response.message || t('scout.flow.download'))
      return
    }
    await downloadMediaFromResult(response.data)
  } catch (error) {
    logError('SCOUT: не удалось скачать выгрузку', error)
    await handleApiError(error, t('scout.flow.download'))
  }
}

async function loadRun(publicId, { quiet = false } = {}) {
  const token = ++loadToken
  if (shownRun !== publicId) {
    records.value = []
    run.value = null
    shownRun = publicId
  }
  if (!quiet) loading.value = true
  try {
    const response = await apiClient.get(scoutEndpoints.run(publicId))
    if (!response.success || token !== loadToken || query.value.run !== publicId) return
    if (finishReturn(response.data)) return
    run.value = response.data
    if (ACTIVE.includes(response.data.status)) schedulePoll()
    if (response.data.record_count) await loadRecords(publicId, token)
    else if (token === loadToken) records.value = []
  } catch (error) {
    logError('SCOUT: не удалось прочитать запуск', error)
  } finally {
    if (token === loadToken && !quiet) loading.value = false
  }
}

async function loadRecords(publicId, token) {
  const response = await apiClient.get(scoutEndpoints.records(publicId))
  if (!response.success || token !== loadToken || query.value.run !== publicId) return
  records.value = response.data.records || []
}

function schedulePoll() {
  window.clearTimeout(pollTimer)
  pollTimer = window.setTimeout(async () => {
    if (!query.value.run) return
    await loadRun(query.value.run, { quiet: true })
  }, 2000)
}
</script>

<template>
  <div class="scout-page container py-4">
    <div class="d-flex justify-content-between align-items-center mb-3">
      <h1 class="h3 mb-0">{{ t('scout.title') }}</h1>
      <button type="button" class="btn btn-outline-secondary btn-sm" :disabled="loading" @click="newJob">{{ t('scout.newJob') }}</button>
    </div>
    <ol class="d-flex flex-wrap gap-3 list-unstyled mb-4">
      <li
        v-for="(step, index) in STEPS"
        :key="step"
        :class="stepMark(step) === 'future' ? 'text-muted' : stepMark(step) === 'current' ? 'fw-bold' : ''"
      >
        {{ index + 1 }}. {{ t(`scout.flow.${step}`) }}
      </li>
    </ol>

    <FormCard v-if="stage === 'setup'" class="mb-4">
      <FormField :label="t('scout.url')" label-for="scout-url">
        <input id="scout-url" v-model="url" class="form-control" type="url">
        <div v-if="urlError" class="text-danger small mt-1">{{ urlError }}</div>
      </FormField>
      <FormField :label="t('scout.flow.optionalTitle')" label-for="scout-title">
        <input id="scout-title" v-model="title" class="form-control" type="text" maxlength="200">
      </FormField>
      <div v-for="(field, index) in fields" :key="index" class="border-top pt-2">
        <FormField :label="t('scout.fieldName')" :label-for="`scout-name-${index}`">
          <input :id="`scout-name-${index}`" v-model="field.name" class="form-control" type="text">
        </FormField>
        <FormField :label="t('scout.fieldSelector')" :label-for="`scout-selector-${index}`">
          <input :id="`scout-selector-${index}`" v-model="field.selector" class="form-control" type="text">
        </FormField>
        <div v-if="fieldErrors[index]" class="text-danger small px-3">{{ fieldErrors[index] }}</div>
        <div class="px-3 pb-2">
          <button type="button" class="btn btn-outline-secondary btn-sm" @click="removeField(index)">
            {{ t('scout.removeField') }}
          </button>
        </div>
      </div>
      <div class="px-3 pb-3">
        <button type="button" class="btn btn-outline-secondary btn-sm" @click="addField">{{ t('scout.addField') }}</button>
      </div>
      <details class="px-3 pb-3">
        <summary>{{ t('scout.flow.rules') }}</summary>
        <div class="d-flex flex-wrap gap-3 py-2">
          <label><input v-model="recordMode" type="radio" value="page"> {{ t('scout.flow.oneRecord') }}</label>
          <label><input v-model="recordMode" type="radio" value="cards"> {{ t('scout.flow.manyCards') }}</label>
        </div>
        <template v-if="recordMode === 'cards'">
          <p class="text-muted">{{ t('scout.flow.cardRowHint') }}</p>
          <FormField :label="t('scout.recordSelector')" label-for="scout-record">
            <div class="d-flex gap-2">
              <SelectBox v-model="recordKind" :options="kindOptions" value-key="id" label-key="name" :include-all-option="false" />
              <input id="scout-record" v-model="recordSelector" class="form-control" type="text">
            </div>
            <div v-if="cardError" class="text-danger small mt-1">{{ cardError }}</div>
          </FormField>
        </template>
        <div v-for="(field, index) in fields" :key="`rule-${index}`" class="border-top pt-2">
          <FormField :label="`${field.name || t('scout.fieldName')}: ${t('scout.fieldKind')}`">
            <SelectBox v-model="field.kind" :options="kindOptions" value-key="id" label-key="name" :include-all-option="false" />
          </FormField>
          <FormField v-if="recordMode === 'cards'" :label="t('scout.fieldScope')">
            <SelectBox v-model="field.scope" :options="scopeOptions" value-key="id" label-key="name" :include-all-option="false" />
          </FormField>
          <FormField :label="t('scout.fieldMany')" :label-for="`scout-many-${index}`">
            <input :id="`scout-many-${index}`" v-model="field.many" type="checkbox">
            <div class="text-muted small">{{ t('scout.flow.manyHint') }}</div>
          </FormField>
        </div>
      </details>
      <template #footer>
        <button type="button" class="btn btn-primary" :disabled="loading" @click="checkPage">
          {{ t('scout.flow.checkPage') }}
        </button>
      </template>
    </FormCard>

    <section v-else-if="stage === 'preview'" class="mb-4">
      <h2 class="h5">{{ run?.status === 'exploring' || returnToSetup ? t('scout.flow.preparing') : t('scout.flow.previewReady') }}</h2>
      <p v-if="run?.status === 'exploring' || returnToSetup" class="d-flex align-items-center gap-2">
        <span class="spinner-border spinner-border-sm" role="status" aria-hidden="true"></span>
        {{ returnToSetup ? t('scout.flow.cancellingLead') : t('scout.flow.preparingLead') }}
      </p>
      <div v-if="run?.status === 'exploring' || returnToSetup" class="border rounded p-4 text-center text-muted">
        {{ t('scout.flow.preparing') }}
      </div>
      <template v-else>
        <p v-if="confirmedNotStarted">{{ t('scout.flow.startHeld') }}</p>
        <p>{{ t('scout.flow.planLead', { url, fields: fields.map((field) => field.name).filter(Boolean).join(', ') }) }}</p>
        <p v-if="sourceRows.length">{{ t('scout.flow.sample', { shown: shownRows.length, total: sourceRows.length }) }}</p>
        <p v-if="gapCount">{{ t('scout.flow.gaps', { count: gapCount }) }}</p>
        <p v-if="!sourceRows.length">{{ t('scout.flow.previewEmpty') }}</p>
        <DataTable
          v-if="sourceRows.length"
          :items="tableRows"
          :columns="columns"
          :empty-text="t('scout.empty')"
          :get-item-key="(item) => item.id"
        />
        <details v-if="evidenceLines.length" class="mt-3">
          <summary>{{ t('scout.flow.evidence') }}</summary>
          <ul><li v-for="(line, index) in evidenceLines" :key="index">{{ line }}</li></ul>
        </details>
      </template>
      <div class="d-flex flex-wrap gap-2 mt-3">
        <button
          v-if="run?.status === 'awaiting_confirmation' && sourceRows.length && !returnToSetup"
          type="button"
          class="btn btn-primary"
          :disabled="loading"
          @click="confirmAndStart"
        >
          {{ confirmedNotStarted ? t('scout.start') : t('scout.flow.confirmStart') }}
        </button>
        <button
          v-if="run?.status === 'awaiting_confirmation' && !sourceRows.length"
          type="button"
          class="btn btn-primary"
          :disabled="loading"
          @click="changeSettings"
        >
          {{ t('scout.flow.fixSettings') }}
        </button>
        <button
          v-if="run?.status === 'awaiting_confirmation' && sourceRows.length"
          type="button"
          class="btn btn-outline-secondary"
          :disabled="loading"
          @click="changeSettings"
        >
          {{ t('scout.flow.changeSettings') }}
        </button>
        <button
          v-if="run?.status === 'exploring'"
          type="button"
          class="btn btn-outline-secondary"
          :disabled="loading"
          @click="changeSettings"
        >
          {{ t('scout.cancel') }}
        </button>
      </div>
    </section>

    <section v-else-if="stage === 'collect'" class="mb-4">
      <h2 class="h5">{{ t(`scout.statuses.${run?.status}`) }}</h2>
      <p v-if="run?.status === 'pausing'">{{ t('scout.flow.pausingLead') }}</p>
      <p v-else-if="run?.status === 'cancelling'">{{ t('scout.flow.cancellingLead') }}</p>
      <p v-else-if="run?.status === 'paused'">{{ t('scout.flow.pausedLead') }}</p>
      <p v-if="pageProgress.fetchTotal || pageProgress.extractTotal">
        <span v-if="pageProgress.fetchTotal">{{ t('scout.flow.loaded', pageProgress) }}</span>
        <template v-if="pageProgress.extractTotal"> · {{ t('scout.flow.extracted', pageProgress) }}</template>
        · {{ t('scout.flow.gotRecords', { records: pageProgress.records }) }}
        <template v-if="pageProgress.errors"> · {{ t('scout.flow.progressErrors', { count: pageProgress.errors }) }}</template>
      </p>
      <p class="text-muted">{{ url }}</p>
      <p v-if="sourceRows.length > shownRows.length">{{ t('scout.flow.recent', { shown: shownRows.length, total: sourceRows.length }) }}</p>
      <p v-if="!records.length">{{ t('scout.flow.waitingRows') }}</p>
      <DataTable
        v-else
        :items="tableRows"
        :columns="columns"
        :empty-text="t('scout.flow.waitingRows')"
        :get-item-key="(item) => item.id"
      />
      <div class="d-flex flex-wrap gap-2 mt-3">
        <button v-if="run?.status === 'running'" type="button" class="btn btn-primary" :disabled="loading" @click="act(scoutEndpoints.pause, t('scout.pause'))">
          {{ t('scout.pause') }}
        </button>
        <button v-if="run?.status === 'paused'" type="button" class="btn btn-primary" :disabled="loading" @click="act(scoutEndpoints.resume, t('scout.flow.resumeCollect'))">
          {{ t('scout.flow.resumeCollect') }}
        </button>
        <button
          v-if="run?.status === 'running' || run?.status === 'paused'"
          type="button"
          class="btn btn-outline-secondary"
          :disabled="loading"
          @click="act(scoutEndpoints.cancel, t('scout.cancel'))"
        >
          {{ t('scout.cancel') }}
        </button>
      </div>
    </section>

    <section v-else class="mb-4">
      <h2 class="h5">
        {{ run?.status === 'completed_with_errors' ? t('scout.flow.finishedErrors') : run?.status === 'failed' ? t('scout.flow.failedLead') : run?.status === 'cancelled' ? t('scout.flow.cancelledLead') : t('scout.flow.finished') }}
      </h2>
      <p v-if="run?.error_summary">{{ run.error_summary }}</p>
      <p v-if="pageProgress.fetchTotal || pageProgress.extractTotal">
        <span v-if="pageProgress.fetchTotal">{{ t('scout.flow.loaded', pageProgress) }}</span>
        <template v-if="pageProgress.extractTotal"> · {{ t('scout.flow.extracted', pageProgress) }}</template>
        · {{ t('scout.flow.gotRecords', { records: pageProgress.records }) }}
        <template v-if="pageProgress.errors"> · {{ t('scout.flow.progressErrors', { count: pageProgress.errors }) }}</template>
      </p>
      <p v-if="!records.length">{{ t('scout.flow.resultEmpty') }}</p>
      <DataTable
        v-else
        :items="tableRows"
        :columns="columns"
        :empty-text="t('scout.empty')"
        :get-item-key="(item) => item.id"
      />
      <div class="d-flex flex-wrap gap-2 align-items-center mt-3">
        <template v-if="records.length && EXPORTABLE.includes(run?.status)">
          <SelectBox v-model="exportKind" :options="exportOptions" value-key="id" label-key="name" :include-all-option="false" />
          <button type="button" class="btn btn-primary" @click="exportFile">{{ t('scout.flow.download') }}</button>
        </template>
        <button type="button" class="btn btn-outline-secondary" @click="clearRunView">{{ t('scout.flow.newCollect') }}</button>
        <button v-if="!records.length" type="button" class="btn btn-primary" @click="clearRunView">{{ t('scout.flow.fixSettings') }}</button>
      </div>
    </section>

    <details v-if="journal.length || events.length">
      <summary>{{ t('scout.flow.details') }}</summary>
      <ul>
        <li v-for="(line, index) in journal" :key="`journal-${index}`">{{ line }}</li>
        <li v-for="(line, index) in events" :key="`event-${index}`">{{ line }}</li>
      </ul>
    </details>
  </div>
</template>
