<script setup>
import { computed, onUnmounted, ref, watch } from 'vue'

import DataTable from '@/components/DataTable.vue'
import FormCard from '@/components/FormCard.vue'
import FormField from '@/components/FormField.vue'
import LoadingContentArea from '@/components/LoadingContentArea.vue'
import SelectBox from '@/components/SelectBox.vue'
import { useRouteQueryState } from '@/composables/useRouteQueryState.js'
import { useAppI18n } from '@/i18n/useAppI18n.js'
import { apiClient } from '@/js/api/manager'
import { handleApiError, showError, showSuccess } from '@/js/utils/toast.js'
import { downloadMediaFromResult } from '@/js/utils/mediaDownload.js'

import { scoutEndpoints } from '../js/endpoints.js'

const { t } = useAppI18n()
const { state: query, patchState } = useRouteQueryState({
  job: { default: '' },
  run: { default: '' },
})

const title = ref('')
const url = ref('')
const recordKind = ref('css')
const recordSelector = ref('')
const fields = ref([{ name: 'title', kind: 'css', selector: '', scope: 'page', many: false }])
const run = ref(null)
const records = ref([])
const loading = ref(false)
const savedStamp = ref('')
let pollTimer = 0
let loadToken = 0
let shownRun = ''

const kindOptions = computed(() => [
  { id: 'css', name: t('scout.fields.css') },
  { id: 'xpath', name: t('scout.fields.xpath') },
])

const scopeOptions = computed(() => [
  { id: 'page', name: t('scout.scopePage') },
  { id: 'record', name: t('scout.scopeRecord') },
])

const activeStatuses = ['exploring', 'running', 'pausing', 'cancelling']
const terminalStatuses = ['cancelled', 'completed', 'completed_with_errors', 'failed']
const statusLabel = computed(() => {
  const status = run.value?.status
  if (!status) return ''
  return t(`scout.statuses.${status}`)
})

const tableRows = computed(() => {
  if (records.value.length) {
    return records.value.map(flattenRecord)
  }
  const preview = run.value?.preview
  if (!Array.isArray(preview)) return []
  return preview.map((row, index) => flattenValues(row.values, index))
})

const columns = computed(() => {
  const keys = new Set()
  tableRows.value.forEach((row) => {
    Object.keys(row).forEach((key) => {
      if (key !== 'id') keys.add(key)
    })
  })
  return [...keys].map((key) => ({ key, label: key }))
})

const tableTitle = computed(() => (
  records.value.length ? t('scout.records') : t('scout.preview')
))

const journal = computed(() => (
  Array.isArray(run.value?.journal) ? run.value.journal : []
))

const events = computed(() => (
  Array.isArray(run.value?.events) ? run.value.events : []
))

const dirty = computed(() => Boolean(query.value.job) && savedStamp.value !== stamp())

watch(() => query.value.job, (publicId) => {
  if (publicId) loadJob(publicId)
}, { immediate: true })

watch(() => query.value.run, (publicId) => {
  if (publicId) loadRun(publicId)
}, { immediate: true })

onUnmounted(() => window.clearTimeout(pollTimer))

function addField() {
  fields.value.push({
    name: '',
    kind: 'css',
    selector: '',
    scope: recordSelector.value.trim() ? 'record' : 'page',
    many: false,
  })
}

function removeField(index) {
  fields.value.splice(index, 1)
}

function payload() {
  const selector = recordSelector.value.trim()
  return {
    title: title.value,
    seed_urls: [url.value],
    record: selector ? { kind: recordKind.value, selector } : null,
    fields: fields.value.map((field) => ({
      name: field.name.trim(),
      kind: field.kind,
      selector: field.selector.trim(),
      scope: field.scope === 'record' ? 'record' : 'page',
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

async function saveJob() {
  loading.value = true
  try {
    const body = payload()
    const existing = query.value.job
    const response = existing
      ? await apiClient.patch(scoutEndpoints.job(existing), body)
      : await apiClient.post(scoutEndpoints.jobs, body)
    if (!response.success) {
      showError(response.message || t('scout.save'))
      return
    }
    patchState({ job: response.data.public_id, run: '' })
    applyJob(response.data)
    savedStamp.value = stamp()
    run.value = null
    records.value = []
    shownRun = ''
    showSuccess(t('scout.save'))
  } catch (error) {
    logError('SCOUT: не удалось сохранить задание', error)
    await handleApiError(error, t('scout.save'))
  } finally {
    loading.value = false
  }
}

function newJob() {
  title.value = ''
  url.value = ''
  recordKind.value = 'css'
  recordSelector.value = ''
  fields.value = [{ name: 'title', kind: 'css', selector: '', scope: 'page', many: false }]
  savedStamp.value = ''
  run.value = null
  records.value = []
  shownRun = ''
  patchState({ job: '', run: '' })
}

async function explore() {
  if (!query.value.job || dirty.value) return
  loading.value = true
  records.value = []
  shownRun = ''
  try {
    const response = await apiClient.post(scoutEndpoints.explore(query.value.job), {})
    if (!response.success) {
      showError(response.message || t('scout.explore'))
      return
    }
    patchState({ run: response.data.public_id })
    run.value = response.data
    schedulePoll()
  } catch (error) {
    logError('SCOUT: не удалось загрузить страницу', error)
    await handleApiError(error, t('scout.explore'))
  } finally {
    loading.value = false
  }
}

async function act(endpoint, label) {
  if (!query.value.run || loading.value) return
  if (dirty.value && endpoint !== scoutEndpoints.resume) return
  const runId = query.value.run
  loading.value = true
  try {
    const response = await apiClient.post(endpoint(runId), {})
    if (!response.success) {
      showError(response.message || label)
      return
    }
    if (query.value.run !== runId) return
    run.value = response.data
    if (activeStatuses.includes(response.data.status)) schedulePoll()
    if (response.data.record_count) await loadRecords(runId, loadToken)
    else records.value = []
  } catch (error) {
    logError('SCOUT: действие со сбором не выполнено', error)
    await handleApiError(error, label)
  } finally {
    loading.value = false
  }
}

async function exportFile(kind) {
  if (!query.value.run) return
  try {
    const response = await apiClient.post(scoutEndpoints.exportRun(query.value.run), { kind })
    if (!response.success) {
      showError(response.message || kind)
      return
    }
    await downloadMediaFromResult(response.data)
  } catch (error) {
    logError('SCOUT: не удалось скачать выгрузку', error)
    await handleApiError(error, kind)
  }
}

async function loadRun(publicId) {
  const token = ++loadToken
  if (shownRun !== publicId) {
    records.value = []
    run.value = null
    shownRun = publicId
  }
  loading.value = true
  try {
    const response = await apiClient.get(scoutEndpoints.run(publicId))
    if (!response.success || token !== loadToken || query.value.run !== publicId) return
    run.value = response.data
    if (activeStatuses.includes(response.data.status)) schedulePoll()
    if (response.data.record_count) await loadRecords(publicId, token)
    else if (token === loadToken) records.value = []
  } catch (error) {
    logError('SCOUT: не удалось прочитать запуск', error)
  } finally {
    if (token === loadToken) loading.value = false
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
    await loadRun(query.value.run)
  }, 2000)
}

function flattenRecord(record) {
  return flattenValues(record.values, record.public_id)
}

function flattenValues(values, id) {
  const row = { id }
  Object.entries(values || {}).forEach(([key, cell]) => {
    row[key] = cell?.normalized || cell?.raw || ''
  })
  return row
}
</script>

<template>
  <div class="scout-page container py-4">
    <h1 class="h3 mb-2">{{ t('scout.title') }}</h1>
    <p class="text-muted mb-4">{{ t('scout.lead') }}</p>

    <FormCard class="mb-4">
      <FormField :label="t('scout.jobTitle')" label-for="scout-title">
        <input id="scout-title" v-model="title" class="form-control" type="text" maxlength="200">
      </FormField>
      <FormField :label="t('scout.url')" label-for="scout-url">
        <input id="scout-url" v-model="url" class="form-control" type="url">
      </FormField>
      <FormField :label="t('scout.recordSelector')" label-for="scout-record">
        <div class="d-flex gap-2">
          <SelectBox
            v-model="recordKind"
            :options="kindOptions"
            value-key="id"
            label-key="name"
            :include-all-option="false"
          />
          <input id="scout-record" v-model="recordSelector" class="form-control" type="text">
        </div>
      </FormField>
      <p class="text-muted px-3">{{ t('scout.recordHint') }}</p>
      <div v-for="(field, index) in fields" :key="index" class="border-top">
        <FormField :label="t('scout.fieldName')" :label-for="`scout-name-${index}`">
          <input :id="`scout-name-${index}`" v-model="field.name" class="form-control" type="text">
        </FormField>
        <FormField :label="t('scout.fieldKind')">
          <SelectBox
            v-model="field.kind"
            :options="kindOptions"
            value-key="id"
            label-key="name"
            :include-all-option="false"
          />
        </FormField>
        <FormField :label="t('scout.fieldScope')">
          <SelectBox
            v-model="field.scope"
            :options="scopeOptions"
            value-key="id"
            label-key="name"
            :include-all-option="false"
          />
        </FormField>
        <FormField :label="t('scout.fieldSelector')" :label-for="`scout-selector-${index}`">
          <input :id="`scout-selector-${index}`" v-model="field.selector" class="form-control" type="text">
        </FormField>
        <FormField :label="t('scout.fieldMany')" :label-for="`scout-many-${index}`">
          <input :id="`scout-many-${index}`" v-model="field.many" type="checkbox">
        </FormField>
        <div class="px-3 pb-3">
          <button type="button" class="btn btn-outline-secondary btn-sm" @click="removeField(index)">
            {{ t('scout.removeField') }}
          </button>
        </div>
      </div>
      <template #footer>
        <button type="button" class="btn btn-outline-secondary" @click="addField">{{ t('scout.addField') }}</button>
        <button type="button" class="btn btn-outline-secondary" :disabled="loading" @click="newJob">{{ t('scout.newJob') }}</button>
        <button type="button" class="btn btn-primary" :disabled="loading" @click="saveJob">{{ t('scout.save') }}</button>
        <button type="button" class="btn btn-primary" :disabled="loading || !query.job || dirty" @click="explore">{{ t('scout.explore') }}</button>
      </template>
    </FormCard>

    <p v-if="dirty" class="mb-3">{{ t('scout.dirty') }}</p>
    <p v-if="statusLabel" class="mb-3">{{ t('scout.status') }}: {{ statusLabel }}</p>
    <section v-if="journal.length" class="mb-3">
      <h2 class="h6">{{ t('scout.journal') }}</h2>
      <ul class="mb-0">
        <li v-for="(line, index) in journal" :key="`journal-${index}`">{{ line }}</li>
      </ul>
    </section>
    <section v-if="events.length" class="mb-3">
      <h2 class="h6">{{ t('scout.events') }}</h2>
      <ul class="mb-0">
        <li v-for="(line, index) in events" :key="`event-${index}`">{{ line }}</li>
      </ul>
    </section>
    <div class="d-flex flex-wrap gap-2 mb-3">
      <button type="button" class="btn btn-primary" :disabled="loading || dirty || run?.status !== 'awaiting_confirmation'" @click="act(scoutEndpoints.approve, t('scout.approve'))">
        {{ t('scout.approve') }}
      </button>
      <button type="button" class="btn btn-primary" :disabled="loading || dirty || run?.status !== 'awaiting_confirmation' || !run?.approved" @click="act(scoutEndpoints.start, t('scout.start'))">
        {{ t('scout.start') }}
      </button>
      <button type="button" class="btn btn-outline-secondary" :disabled="loading || run?.status !== 'running'" @click="act(scoutEndpoints.pause, t('scout.pause'))">
        {{ t('scout.pause') }}
      </button>
      <button type="button" class="btn btn-outline-secondary" :disabled="loading || run?.status !== 'paused'" @click="act(scoutEndpoints.resume, t('scout.resume'))">
        {{ t('scout.resume') }}
      </button>
      <button type="button" class="btn btn-outline-secondary" :disabled="loading || !run || terminalStatuses.includes(run.status)" @click="act(scoutEndpoints.cancel, t('scout.cancel'))">
        {{ t('scout.cancel') }}
      </button>
      <button type="button" class="btn btn-outline-secondary" :disabled="!records.length" @click="exportFile('json')">{{ t('scout.exportJson') }}</button>
      <button type="button" class="btn btn-outline-secondary" :disabled="!records.length" @click="exportFile('csv')">{{ t('scout.exportCsv') }}</button>
    </div>

    <h2 class="h5">{{ tableTitle }}</h2>
    <LoadingContentArea :loading="loading">
      <DataTable
        :items="tableRows"
        :columns="columns"
        :empty-text="t('scout.empty')"
        :get-item-key="(item) => item.id"
      />
    </LoadingContentArea>
  </div>
</template>
