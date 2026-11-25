<script setup>
import { computed, ref, toRef, useId, watch } from 'vue'
import FormCard from '@/components/FormCard.vue'
import FormField from '@/components/FormField.vue'
import { useAppI18n } from '@/i18n/useAppI18n.js'
import { useScoutRunHistory } from '../js/useScoutRunHistory.js'
const props = defineProps({ jobPublicId: String, targetRunPublicId: String, disabled: Boolean })
const emit = defineEmits(['select', 'job-change'])
const { t, locale } = useAppI18n(),
  uid = useId()
const {
  jobId,
  jobs,
  rows,
  cursor,
  loading,
  jobsLoading,
  loaded,
  error,
  jobsError,
  chooseJob,
  load,
  loadJobs,
} = useScoutRunHistory(toRef(props, 'jobPublicId'))
const base = ref(''),
  target = ref(props.targetRunPublicId || '')
watch(jobId, () => {
  base.value = ''
  target.value = props.targetRunPublicId || ''
})
watch(
  () => props.targetRunPublicId,
  (id) => {
    target.value = id || ''
  },
)
const eligible = (id) => rows.value.find((row) => row.public_id === id && row.comparison_eligible)
const canApply = computed(
  () =>
    !props.disabled &&
    !loading.value &&
    base.value !== target.value &&
    !!eligible(base.value) &&
    !!eligible(target.value),
)
function changeJob(event) {
  chooseJob(event.target.value)
  emit('job-change', event.target.value)
}
function select() {
  if (canApply.value)
    emit('select', {
      base: base.value,
      target: target.value,
      fields: eligible(target.value).comparison_fields,
      job: jobId.value,
    })
}
function time(row) {
  if (!row.started_at) return t('scout.comparison.history.notStarted')
  const date = new Date(row.started_at)
  return Number.isNaN(date.valueOf())
    ? '—'
    : new Intl.DateTimeFormat(locale.value, {
        year: 'numeric',
        month: 'short',
        day: 'numeric',
        hour: '2-digit',
        minute: '2-digit',
        second: '2-digit',
      }).format(date)
}
const h = (key) => t(`scout.comparison.history.${key}`)
</script>
<template>
  <FormCard class="scout-comparison-card scout-history">
    <section class="scout-comparison-content" :aria-labelledby="`${uid}-title`">
      <h2 :id="`${uid}-title`" class="h5">{{ h('title') }}</h2>
      <p>{{ h('lead') }}</p>
      <FormField :label="h('job')" :label-for="`${uid}-job`" :hint="h('jobHint')">
        <select
          :id="`${uid}-job`"
          class="form-select"
          :value="jobId"
          :disabled="disabled || jobsLoading"
          @change="changeJob"
        >
          <option value="">{{ h('chooseJob') }}</option>
          <option v-if="jobId && !jobs.some((job) => job.public_id === jobId)" :value="jobId">
            {{ h('currentJob') }}
          </option>
          <option v-for="job in jobs" :key="job.public_id" :value="job.public_id">
            {{ job.title }}
          </option>
        </select>
      </FormField>
      <div v-if="jobsError" role="alert" class="alert alert-warning">
        <p>{{ h(`errors.${jobsError.key}`) }}</p>
        <button
          class="btn btn-outline-secondary"
          type="button"
          :disabled="jobsLoading"
          @click="loadJobs"
        >
          {{ h('retryJobs') }}
        </button>
      </div>
      <button
        v-if="jobId"
        class="btn btn-outline-secondary"
        type="button"
        :disabled="loading || disabled"
        @click="load(false)"
      >
        {{ h('refresh') }}
      </button>
      <p v-if="loading" role="status">{{ h('loading') }}</p>
      <div v-if="error" role="alert" class="alert alert-warning mt-3">
        <p>{{ h(`errors.${error.key}`) }}</p>
        <button
          class="btn btn-outline-secondary"
          type="button"
          :disabled="loading"
          @click="load(!!cursor)"
        >
          {{ h('retry') }}
        </button>
      </div>
      <p v-if="loaded && !rows.length && !loading">{{ h('empty') }}</p>
      <div v-if="rows.length" class="table-responsive mt-3">
        <table class="table align-middle">
          <thead>
            <tr>
              <th scope="col">{{ h('time') }}</th>
              <th scope="col">{{ h('state') }}</th>
              <th scope="col">{{ h('version') }}</th>
              <th scope="col">{{ h('count') }}</th>
              <th scope="col">{{ h('base') }}</th>
              <th scope="col">{{ h('target') }}</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="row in rows" :key="row.public_id" :data-run="row.public_id">
              <td>
                <span class="scout-history-mobile-label" aria-hidden="true">{{ h('time') }}</span>
                <RouterLink
                  :to="{ path: '/scout', query: { job: jobId, run: row.public_id } }"
                  :title="h('open')"
                  >{{ time(row) }}</RouterLink
                >
              </td>
              <td>
                <span class="scout-history-mobile-label" aria-hidden="true">{{ h('state') }}</span>
                {{ t(`scout.statuses.${row.status}`)
                }}<span v-if="row.completeness" class="d-block small">{{
                  t(`scout.comparison.${row.completeness === 'complete' ? 'complete' : 'partial'}`)
                }}</span
                ><span v-if="row.comparison_blocked_reason" class="d-block small scout-muted">{{
                  h(`reasons.${row.comparison_blocked_reason}`)
                }}</span>
              </td>
              <td>
                <span class="scout-history-mobile-label" aria-hidden="true">{{ h('version') }}</span
                >{{ row.revision_number }}
              </td>
              <td>
                <span class="scout-history-mobile-label" aria-hidden="true">{{ h('count') }}</span
                >{{ row.record_count }}
              </td>
              <td>
                <span class="scout-history-mobile-label" aria-hidden="true">{{ h('base') }}</span>
                <input
                  v-model="base"
                  class="form-check-input"
                  type="radio"
                  :name="`${uid}-base`"
                  :value="row.public_id"
                  :disabled="disabled || loading || !row.comparison_eligible"
                  :aria-label="`${h('base')} · ${time(row)} · ${row.public_id}`"
                />
              </td>
              <td>
                <span class="scout-history-mobile-label" aria-hidden="true">{{ h('target') }}</span>
                <input
                  v-model="target"
                  class="form-check-input"
                  type="radio"
                  :name="`${uid}-target`"
                  :value="row.public_id"
                  :disabled="disabled || loading || !row.comparison_eligible"
                  :aria-label="`${h('target')} · ${time(row)} · ${row.public_id}`"
                />
              </td>
            </tr>
          </tbody>
        </table>
      </div>
      <p v-if="loaded" class="small scout-muted">
        {{ t('scout.comparison.history.loaded', { count: rows.length }) }}
      </p>
      <div class="scout-comparison-buttons">
        <button
          v-if="cursor"
          class="btn btn-outline-secondary"
          type="button"
          :disabled="loading || disabled"
          @click="load(true)"
        >
          {{ h('more') }}
        </button>
        <button
          v-if="rows.length"
          class="btn btn-primary"
          type="button"
          :disabled="!canApply"
          @click="select"
        >
          {{ h('apply') }}
        </button>
      </div>
      <p class="small scout-muted mt-3">{{ h('versionHint') }}</p>
    </section>
  </FormCard>
</template>
<style scoped>
.scout-history-mobile-label {
  display: none;
}
@media (max-width: 700px) {
  table {
    display: block;
    margin-bottom: 0;
  }
  thead {
    position: absolute;
    width: 1px;
    height: 1px;
    overflow: hidden;
    clip-path: inset(50%);
  }
  tbody {
    display: grid;
    gap: 1rem;
  }
  tbody tr {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    border: 1px solid var(--ui-border);
    border-radius: var(--ui-radius-sm);
  }
  td {
    display: block;
    min-width: 0;
    border: 0;
  }
  .scout-history-mobile-label {
    display: block;
    margin-bottom: 0.35rem;
    color: var(--ui-text-muted);
    font-size: 0.75rem;
  }
}
</style>
