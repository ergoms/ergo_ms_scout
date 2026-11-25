<script setup>
import { onMounted, ref } from 'vue'
import { TriangleAlert } from '@lucide/vue'
import FormCard from '@/components/FormCard.vue'
import { useAppI18n } from '@/i18n/useAppI18n.js'
import { retentionBytes, retentionCount } from '../js/retentionState.js'
import { scheduleMoment } from '../js/scheduleState.js'

defineProps({
  preview: { type: Object, required: true },
  disabled: Boolean,
  problem: { type: String, default: '' },
})
const emit = defineEmits(['cancel', 'confirm'])
const { t, locale } = useAppI18n()
const heading = ref(null)
onMounted(() => heading.value?.focus())
const count = (n) => retentionCount(n, locale.value)
const bytes = (n) => (n == null ? t('scout.retention.unknown') : retentionBytes(n, locale.value))
</script>

<template>
  <div class="retention-review" @keydown.esc.prevent="emit('cancel')">
    <FormCard>
      <section>
        <h2 ref="heading" tabindex="-1">{{ t('scout.retention.deleteTitle') }}</h2>
        <p class="small scout-muted">
          {{ t('scout.retention.evaluated') }} ·
          {{ scheduleMoment(preview.evaluated_at, 'UTC', locale) }} UTC
        </p>
        <div class="alert alert-danger">
          <TriangleAlert :size="18" aria-hidden="true" />
          <div>
            <strong>{{ t('scout.retention.irreversible') }}</strong>
            <p>{{ t('scout.retention.reviewWarning') }}</p>
          </div>
        </div>
        <table class="retention-review-table">
          <thead>
            <tr>
              <th scope="col">{{ t('scout.retention.deleteColumn') }}</th>
              <th scope="col">{{ t('scout.retention.quantity') }}</th>
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="key in [
                'runs_with_result_deletion',
                'records',
                'evidence',
                'html_files',
                'export_files',
                'visual_snapshots',
              ]"
              :key="key"
            >
              <th scope="row">{{ t(`scout.retention.totals.${key}`) }}</th>
              <td>{{ count(preview.totals[key]) }}</td>
            </tr>
            <tr v-if="preview.totals.images > 0">
              <th scope="row">{{ t('scout.retention.imagesLabel') }}</th>
              <td>{{ count(preview.totals.images) }} · {{ bytes(preview.totals.image_bytes) }}</td>
            </tr>
            <tr>
              <th scope="row">{{ t('scout.retention.totals.file_bytes_reclaimable') }}</th>
              <td>{{ bytes(preview.totals.file_bytes_reclaimable) }}</td>
            </tr>
          </tbody>
        </table>
        <p v-if="problem" role="status" class="alert alert-warning">{{ problem }}</p>
      </section>
    </FormCard>
    <footer class="retention-policy-actions">
      <button class="btn btn-outline-secondary" type="button" @click="emit('cancel')">
        {{ t('scout.retention.cancel') }}
      </button>
      <button
        class="btn btn-outline-danger"
        type="button"
        :disabled="disabled"
        @click="emit('confirm')"
      >
        {{ t('scout.retention.deleteConfirm') }}
      </button>
    </footer>
  </div>
</template>
