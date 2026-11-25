<script setup>
import { computed, nextTick, ref, watch } from 'vue'
import DataTable from '@/components/DataTable.vue'
import { useRouteQueryState } from '@/composables/useRouteQueryState.js'
import { useAppI18n } from '@/i18n/useAppI18n.js'
import { displayValue } from '../js/recordTable.js'
import { scoutEndpoints } from '../js/endpoints.js'
import ScoutImageDownload from './ScoutImageDownload.vue'

const props = defineProps({
  records: { type: Array, default: () => [] },
  preview: { type: Boolean, default: false },
  runId: { type: String, default: '' },
})
const { t } = useAppI18n()
const selected = ref(null)
const evidenceHeading = ref(null)
let openingButton = null
const { state: query, patchState } = useRouteQueryState(
  { page: { default: 1, type: 'number' } },
  { preserveKeys: ['job', 'run', 'mode'] },
)
const page = computed({
  get: () =>
    Math.max(1, Math.min(Number(query.value.page) || 1, Math.ceil(props.records.length / 10) || 1)),
  set: (value) => patchState({ page: value }),
})
const items = computed(() =>
  props.records.map((record, index) => ({ key: record.public_id || index, record })),
)
const columns = computed(() =>
  [...new Set(props.records.flatMap((record) => Object.keys(record.values || {})))].map((key) => ({
    key,
    label: key,
  })),
)
const selectedValue = computed(() => selected.value?.record.values?.[selected.value?.field])
const evidence = computed(() =>
  (selected.value?.record.evidence || []).filter(
    (item) => item.field_path === selected.value?.field,
  ),
)
watch(
  () => props.preview,
  () => {
    selected.value = null
    patchState({ page: 1 })
  },
)

watch(
  () => props.records,
  () => {
    if (!selected.value) return
    const record = props.records.find(
      (item) => item.public_id && item.public_id === selected.value.record.public_id,
    )
    selected.value = record ? { record, field: selected.value.field } : null
  },
)

async function selectCell(item, field, event) {
  openingButton = event.currentTarget
  selected.value = { record: item.record, field }
  await nextTick()
  evidenceHeading.value?.focus()
}

function closeEvidence() {
  selected.value = null
  if (openingButton?.isConnected) openingButton.focus()
  openingButton = null
}

function cellLabel(cell) {
  return displayValue(cell, t('scout.flow.notFound'))
}
</script>

<template>
  <div class="scout-records">
    <DataTable
      v-model:current-page="page"
      :items="items"
      :columns="columns"
      :get-item-key="(item) => item.key"
      :empty-text="t('scout.empty')"
      :enable-pagination="!preview"
      :items-per-page="10"
      cards-below="sm"
    >
      <template v-for="column in columns" :key="column.key" #[`cell-${column.key}`]="{ item }">
        <button
          v-if="!item.record.values?.[column.key]?.images?.length"
          type="button"
          class="scout-value"
          :class="{
            'scout-value--selected':
              selected?.record === item.record && selected?.field === column.key,
            'scout-badge scout-badge--missing': Array.isArray(
              item.record.values?.[column.key]?.images,
            ),
          }"
          :aria-label="
            t('scout.workspace.valueOrigin', {
              field: column.label,
              value: cellLabel(item.record.values?.[column.key]),
            })
          "
          @click="selectCell(item, column.key, $event)"
        >
          {{ cellLabel(item.record.values?.[column.key]) }}
          <small v-if="item.record.values?.[column.key]?.typed" class="d-block scout-muted">{{
            t(`scout.product.typedStatus.${item.record.values[column.key].typed.status}`)
          }}</small>
        </button>
        <ul v-else class="scout-cell-images">
          <li
            v-for="(image, index) in item.record.values[column.key].images"
            :key="image.public_id || index"
          >
            <button
              type="button"
              class="scout-image-status"
              :title="image.source_url"
              :aria-label="
                t('scout.workspace.valueOrigin', {
                  field: column.label,
                  value: t(`scout.visual.image.status.${image.status}`) + ' · ' + image.source_url,
                })
              "
              @click="selectCell(item, column.key, $event)"
            >
              <span class="scout-badge" :class="`scout-badge--${image.status}`">{{
                t(`scout.visual.image.status.${image.status}`)
              }}</span>
            </button>
            <ScoutImageDownload
              v-if="runId && image.status === 'saved' && image.public_id"
              :key="`${runId}/${image.public_id}`"
              :endpoint="scoutEndpoints.runImage(runId, image.public_id)"
              :filename-stem="`scout-${image.public_id}`"
              saved
            />
          </li>
        </ul>
      </template>
    </DataTable>
    <section
      v-if="selected"
      class="scout-evidence"
      :aria-label="t('scout.flow.evidence')"
      @keydown.esc.stop="closeEvidence"
    >
      <div class="scout-section-heading">
        <h3 ref="evidenceHeading" class="h6 mb-0" tabindex="-1">
          {{ t('scout.flow.evidence') }}: {{ selected.field }}
        </h3>
        <button
          type="button"
          class="btn btn-sm btn-outline-secondary"
          :aria-label="t('scout.workspace.closeEvidence')"
          @click="closeEvidence"
        >
          {{ t('common.close') }}
        </button>
      </div>
      <dl v-if="selectedValue" class="scout-evidence__values">
        <div v-if="selectedValue.images?.length" class="scout-evidence__wide">
          <dt>{{ t('scout.url') }}</dt>
          <dd>
            <ul class="list-unstyled mb-0">
              <li v-for="(image, index) in selectedValue.images" :key="image.public_id || index">
                {{ image.source_url }}
              </li>
            </ul>
          </dd>
        </div>
        <div v-else>
          <dt>{{ t('scout.workspace.rawValue') }}</dt>
          <dd>{{ selectedValue.raw || t('scout.flow.notFound') }}</dd>
        </div>
        <div v-if="!selectedValue.images?.length">
          <dt>{{ t('scout.product.normalized') }}</dt>
          <dd>{{ selectedValue.normalized || t('scout.flow.notFound') }}</dd>
        </div>
        <div v-if="selectedValue.typed">
          <dt>{{ t('scout.product.typedValue') }}</dt>
          <dd>
            {{
              selectedValue.typed.status === 'ok'
                ? displayValue(selectedValue, t('scout.flow.notFound'))
                : t(`scout.product.typedStatus.${selectedValue.typed.status}`)
            }}
          </dd>
        </div>
        <div v-if="selectedValue.typed?.error?.code">
          <dt>{{ t('scout.product.errorCode') }}</dt>
          <dd>{{ selectedValue.typed.error.code }}</dd>
        </div>
      </dl>
      <div v-for="(item, index) in evidence" :key="index" class="scout-observation">
        <div v-if="item.selected || item.typed?.status" class="scout-observation__badges">
          <span v-if="item.selected" class="scout-badge scout-badge--candidate">{{
            t('scout.design.activeObservation')
          }}</span>
          <span
            v-if="item.typed?.status"
            class="scout-badge"
            :class="item.typed.status === 'ok' ? 'scout-badge--saved' : 'scout-badge--failed'"
            >{{ t(`scout.product.typedStatus.${item.typed.status}`) }}</span
          >
        </div>
        <dl class="scout-evidence__details">
          <div class="scout-evidence__wide">
            <dt>{{ t('scout.url') }}</dt>
            <dd>
              {{
                item.source_url || selected.record.source_url || t('scout.workspace.notSpecified')
              }}
            </dd>
          </div>
          <div class="scout-evidence__wide">
            <dt>{{ t('scout.fieldSelector') }}</dt>
            <dd>
              <code>{{ item.locator || t('scout.workspace.notSpecified') }}</code>
            </dd>
          </div>
          <div v-if="evidence.length > 1 || item.raw_value !== selectedValue?.raw">
            <dt>{{ t('scout.workspace.rawValue') }}</dt>
            <dd>{{ item.raw_value ?? t('scout.flow.notFound') }}</dd>
          </div>
          <div>
            <dt>{{ t('scout.product.snapshot') }}</dt>
            <dd>{{ item.snapshot_public_id || t('scout.workspace.notSpecified') }}</dd>
          </div>
          <div>
            <dt>{{ t('scout.product.revision') }}</dt>
            <dd>{{ item.rules_revision_public_id || t('scout.workspace.notSpecified') }}</dd>
          </div>
        </dl>
      </div>
      <p v-if="!evidence.length" class="scout-muted small mt-2 mb-0">
        {{ t('scout.workspace.noEvidence') }}
      </p>
    </section>
  </div>
</template>
