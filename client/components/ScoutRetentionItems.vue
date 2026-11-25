<script setup>
import { useAppI18n } from '@/i18n/useAppI18n.js'
import { retentionCount, retentionBytes } from '../js/retentionState.js'
import { scheduleMoment } from '../js/scheduleState.js'
import messages from '../js/retentionLocales.js'
defineProps({ items: { type: Array, default: () => [] } })
const { t, locale } = useAppI18n()
function label(group, key) {
  return t(
    Object.hasOwn(messages.ru[group], key)
      ? `scout.retention.${group}.${key}`
      : 'scout.retention.readOnlyUnknown',
  )
}
</script>
<template>
  <div v-if="items.length" class="retention-item-table">
    <div class="retention-table-head" aria-hidden="true">
      <span>{{ t('scout.retention.object') }}</span>
      <span>{{ t('scout.retention.material') }}</span>
      <span>{{ t('scout.retention.decision') }}</span>
      <span>{{ t('scout.retention.reason') }}</span>
    </div>
    <ol class="scout-retention-items">
      <li v-for="(item, index) in items" :key="`${item.kind}-${item.public_id}-${index}`">
        <div class="retention-object-id">
          <code>{{ item.public_id }}</code>
        </div>
        <div>{{ label('scopes', item.kind) }}</div>
        <span
          class="retention-status"
          :data-state="item.status || (item.reason ? 'excluded' : 'candidate')"
        >
          {{
            item.status
              ? label('statuses', item.status)
              : t(item.reason ? 'scout.retention.excluded' : 'scout.retention.candidate')
          }}
        </span>
        <div>
          <span v-if="item.reason">{{ label('reasons', item.reason) }}</span>
          <span v-else-if="item.eligible_at"
            >{{ t('scout.retention.eligible') }} ·
            {{ scheduleMoment(item.eligible_at, 'UTC', locale) }} UTC</span
          >
          <span v-else>—</span>
          <details>
            <summary>{{ t('scout.retention.itemDetails') }}</summary>
            <dl class="scout-retention-details">
              <div v-if="item.run_public_id">
                <dt>{{ t('scout.retention.run') }}</dt>
                <dd>
                  <code>{{ item.run_public_id }}</code>
                </dd>
              </div>
              <div v-if="item.eligible_at">
                <dt>{{ t('scout.retention.eligible') }}</dt>
                <dd>{{ scheduleMoment(item.eligible_at, 'UTC', locale) }} UTC</dd>
              </div>
              <div
                v-for="key in ['records', 'evidence', 'export_files'].filter((key) =>
                  Object.hasOwn(item, key),
                )"
                :key="key"
              >
                <dt>{{ t(`scout.retention.${key === 'export_files' ? 'exportFiles' : key}`) }}</dt>
                <dd>{{ retentionCount(item[key], locale) }}</dd>
              </div>
              <div v-if="Object.hasOwn(item, 'bytes')">
                <dt>{{ t('scout.retention.bytes') }}</dt>
                <dd>
                  {{
                    item.bytes === null
                      ? t('scout.retention.unknown')
                      : retentionBytes(item.bytes, locale)
                  }}
                </dd>
              </div>
              <div v-if="Object.hasOwn(item, 'deleted_bytes')">
                <dt>{{ t('scout.retention.deletedBytes') }}</dt>
                <dd>{{ retentionCount(item.deleted_bytes, locale) }}</dd>
              </div>
              <div v-if="Object.hasOwn(item, 'deleted_rows')">
                <dt>{{ t('scout.retention.deletedRows') }}</dt>
                <dd>{{ retentionCount(item.deleted_rows, locale) }}</dd>
              </div>
            </dl>
          </details>
        </div>
        <p v-if="item.comparison_will_expire" class="scout-warning">
          {{ t('scout.retention.itemComparison') }}
        </p>
        <p v-if="item.executable === false" class="scout-warning">
          {{ t('scout.retention.itemUnavailable') }}
        </p>
      </li>
    </ol>
  </div>
</template>
