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
const fields = ref([{ name: 'title', kind: 'css', selector: '', many: true }])
const run = ref(null)
const records = ref([])
const loading = ref(false)
let pollTimer = 0

const kindOptions = computed(() => [
  { id: 'css', name: t('scout.fields.css') },
  { id: 'xpath', name: t('scout.fields.xpath') },
])

const activeStatuses = ['exploring', 'running', 'pausing', 'cancelling']
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

watch(() => query.value.run, (publicId) => {
  if (publicId) loadRun(publicId)
}, { immediate: true })

onUnmounted(() => window.clearTimeout(pollTimer))

function addField() {
  fields.value.push({ name: '', kind: 'css', selector: '', many: false })
}

function removeField(index) {
  fields.value.splice(index, 1)
}

function payload() {
  return {
    title: title.value,
    seed_urls: [url.value],
    fields: fields.value.map((field) => ({
      name: field.name.trim(),
      kind: field.kind,
      selector: field.selector.trim(),
      many: field.many,
    })),
  }
}

async function createJob() {
  loading.value = true
  try {
    const response = await apiClient.post(scoutEndpoints.jobs, payload())
    if (!response.success) {
      showError(response.message || t('scout.create'))
      return
    }
    patchState({ job: response.data.public_id, run: '' })
    run.value = null
    records.value = []
    showSuccess(t('scout.create'))
  } catch (error) {
    logError('SCOUT: не удалось сохранить задание', error)
    await handleApiError(error, t('scout.create'))
  } finally {
    loading.value = false
  }
}

async function explore() {
  if (!query.value.job) return
  loading.value = true
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
  if (!query.value.run) return
  loading.value = true
  try {
    const response = await apiClient.post(endpoint(query.value.run), {})
    if (!response.success) {
      showError(response.message || label)
      return
    }
    run.value = response.data
    if (activeStatuses.includes(response.data.status)) schedulePoll()
    if (response.data.status === 'completed' || response.data.status === 'completed_with_errors') {
      await loadRecords()
    }
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
  loading.value = true
  try {
    const response = await apiClient.get(scoutEndpoints.run(publicId))
    if (!response.success) return
    run.value = response.data
    if (activeStatuses.includes(response.data.status)) schedulePoll()
    if (response.data.record_count) await loadRecords()
  } catch (error) {
    logError('SCOUT: не удалось прочитать запуск', error)
  } finally {
    loading.value = false
  }
}

async function loadRecords() {
  const response = await apiClient.get(scoutEndpoints.records(query.value.run))
  if (response.success) records.value = response.data.records || []
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
        <button type="button" class="btn btn-primary" :disabled="loading" @click="createJob">{{ t('scout.create') }}</button>
        <button type="button" class="btn btn-primary" :disabled="loading || !query.job" @click="explore">{{ t('scout.explore') }}</button>
      </template>
    </FormCard>

    <p v-if="statusLabel" class="mb-3">{{ t('scout.status') }}: {{ statusLabel }}</p>
    <div class="d-flex flex-wrap gap-2 mb-3">
      <button type="button" class="btn btn-primary" :disabled="run?.status !== 'awaiting_confirmation'" @click="act(scoutEndpoints.approve, t('scout.approve'))">
        {{ t('scout.approve') }}
      </button>
      <button type="button" class="btn btn-primary" :disabled="run?.status !== 'awaiting_confirmation' || !run?.approved" @click="act(scoutEndpoints.start, t('scout.start'))">
        {{ t('scout.start') }}
      </button>
      <button type="button" class="btn btn-outline-secondary" :disabled="run?.status !== 'running'" @click="act(scoutEndpoints.pause, t('scout.pause'))">
        {{ t('scout.pause') }}
      </button>
      <button type="button" class="btn btn-outline-secondary" :disabled="!run" @click="act(scoutEndpoints.cancel, t('scout.cancel'))">
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
