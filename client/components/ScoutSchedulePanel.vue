<script setup>
import { computed, ref, useId } from 'vue'
import FormCard from '@/components/FormCard.vue'
import FormField from '@/components/FormField.vue'
import { confirmAction } from '@/js/utils/confirm.js'
import { useAppI18n } from '@/i18n/useAppI18n.js'
import { useScoutSchedule } from '../js/useScoutSchedule.js'
import {
  OCCURRENCE_STATUSES,
  OCCURRENCE_REASONS,
  sameSchedulePlan,
  validScheduleDraft,
  scheduleDate,
  scheduleMoment,
} from '../js/scheduleState.js'
const props = defineProps({
  jobId: { type: String, required: true },
  plan: { type: Object, default: null },
  planDirty: Boolean,
  disabled: Boolean,
})
const { t, locale } = useAppI18n()
const uid = useId(),
  confirming = ref(false),
  notice = ref('')
const {
  current,
  state,
  error,
  forbidden,
  writing,
  needsReview,
  saved,
  draft,
  dirty,
  candidateReady,
  canWrite,
  history,
  historyCursor,
  historyError,
  historyLoading,
  reload,
  readHistory,
  resolveConflict,
  write,
} = useScoutSchedule(
  computed(() => props.jobId),
  computed(() => props.plan),
  computed(() => props.planDirty),
)
defineExpose({ dirty, writing, confirming })
const blocked = computed(
  () =>
    props.disabled ||
    confirming.value ||
    writing.value ||
    state.value === 'loading' ||
    needsReview.value ||
    forbidden.value,
)
const formReady = computed(
  () =>
    !blocked.value &&
    canWrite.value &&
    validScheduleDraft(draft) &&
    (current.value || candidateReady.value),
)
const differentPlan = computed(
  () => !!current.value && !!props.plan && !sameSchedulePlan(current.value, props.plan),
)
const canSave = computed(() => formReady.value && (!current.value || dirty.value))
const canRebind = computed(() => formReady.value && differentPlan.value && candidateReady.value)
const planName = computed(() =>
  !current.value || sameSchedulePlan(current.value, props.plan)
    ? props.plan?.revision_number
      ? t('scout.schedule.revision', { number: props.plan.revision_number })
      : t('scout.schedule.currentPlan')
    : t('scout.schedule.olderPlan'),
)
const moment = (value) => scheduleMoment(value, current.value?.timezone || 'UTC', locale.value)
function actionStamp() {
  return JSON.stringify({
    job: props.jobId,
    version: current.value?.version,
    plan: props.plan,
    dirtyPlan: props.planDirty,
    draft,
  })
}
async function save(bindPlan = false) {
  if (!(bindPlan ? canRebind.value : canSave.value)) return
  notice.value = ''
  const before = actionStamp()
  if (draft.enabled) {
    confirming.value = true
    let confirmed
    try {
      confirmed = await confirmAction({
        title: t('scout.schedule.confirmTitle'),
        message: t('scout.schedule.confirmMessage', {
          time: draft.local_time,
          zone: draft.timezone,
          plan: bindPlan
            ? t('scout.schedule.revision', { number: props.plan.revision_number || '—' })
            : planName.value,
        }),
        confirmText: t('scout.schedule.confirmEnable'),
        cancelText: t('scout.schedule.cancel'),
        variant: 'primary',
      })
    } finally {
      confirming.value = false
    }
    if (!confirmed) return
    if (before !== actionStamp()) {
      notice.value = t('scout.schedule.stale')
      return
    }
  }
  await write({ bindPlan })
}
</script>
<template>
  <div class="scout-schedule">
    <details>
      <summary class="scout-schedule-summary">
        <strong>{{ t('scout.schedule.title') }}</strong
        ><span
          v-if="current"
          class="schedule-pill"
          :data-tone="current.enabled ? 'success' : 'neutral'"
          >{{ t(current.enabled ? 'scout.schedule.on' : 'scout.schedule.off') }}</span
        >
      </summary>
      <div class="scout-schedule-content">
        <p>{{ t('scout.schedule.lead') }}</p>
        <p v-if="state === 'loading'" role="status">{{ t('scout.schedule.loading') }}</p>
        <p v-if="state === 'absent'" class="scout-muted">{{ t('scout.schedule.absent') }}</p>
        <div v-if="error" class="alert alert-warning" role="alert">
          <p class="mb-0">{{ t(`scout.schedule.errors.${error.key}`) }}</p>
          <p v-if="error.detail" class="small mb-0">{{ error.detail }}</p>
        </div>
        <p v-if="notice" class="alert alert-warning" role="status">{{ notice }}</p>
        <p v-if="saved && !dirty" role="status">{{ t('scout.schedule.saved') }}</p>
        <div v-if="needsReview && current" class="scout-schedule-conflict" role="status">
          <p>{{ t('scout.schedule.conflict') }}</p>
          <strong
            >{{ t('scout.schedule.serverValues') }}: {{ current.local_time }} ·
            {{ current.timezone }} ·
            {{ t(current.enabled ? 'scout.schedule.on' : 'scout.schedule.off') }}</strong
          >
          <div class="scout-schedule-buttons">
            <button
              type="button"
              class="btn btn-outline-secondary"
              :disabled="writing"
              @click="resolveConflict(true)"
            >
              {{ t('scout.schedule.takeServer') }}</button
            ><button
              type="button"
              class="btn btn-outline-secondary"
              :disabled="writing"
              @click="resolveConflict(false)"
            >
              {{ t('scout.schedule.keepMine') }}
            </button>
          </div>
        </div>
        <div class="schedule-grid">
          <FormCard
            ><section class="schedule-card">
              <h3>{{ t('scout.schedule.time') }}</h3>
              <p v-if="planDirty" class="alert alert-info">{{ t('scout.schedule.unsavedPlan') }}</p>
              <p v-else-if="!candidateReady" class="scout-muted">
                {{ t('scout.schedule.planRequired') }}
              </p>
              <form :id="`${uid}-schedule-form`" @submit.prevent="save(false)">
                <fieldset :disabled="blocked || !['ready', 'absent'].includes(state)">
                  <legend class="visually-hidden">{{ t('scout.schedule.title') }}</legend>
                  <div class="scout-schedule-fields">
                    <FormField :label="t('scout.schedule.time')" :label-for="`${uid}-time`"
                      ><input
                        :id="`${uid}-time`"
                        v-model="draft.local_time"
                        type="time"
                        step="60"
                        class="form-control"
                        required
                        :aria-invalid="error?.path === '/local_time'"
                    /></FormField>
                    <FormField
                      :label="t('scout.schedule.zone')"
                      :label-for="`${uid}-zone`"
                      :hint="t('scout.schedule.zoneHint')"
                      ><input
                        :id="`${uid}-zone`"
                        v-model="draft.timezone"
                        :list="`${uid}-zones`"
                        class="form-control"
                        maxlength="80"
                        required
                        :aria-invalid="error?.path === '/timezone'" /><datalist
                        :id="`${uid}-zones`"
                      >
                        <option
                          v-for="zone in [
                            'UTC',
                            'Europe/Moscow',
                            'Europe/Berlin',
                            'Asia/Almaty',
                            'Asia/Vladivostok',
                            'America/New_York',
                          ]"
                          :key="zone"
                          :value="zone"
                        /></datalist
                    ></FormField>
                  </div>
                  <label class="scout-schedule-consent" :for="`${uid}-enabled`"
                    ><input :id="`${uid}-enabled`" v-model="draft.enabled" type="checkbox" />{{
                      t('scout.schedule.enabled')
                    }}</label
                  >
                  <p v-if="current && dirty" class="small scout-warning">
                    {{ t('scout.schedule.dirty') }}
                  </p>
                </fieldset>
              </form>
              <dl v-if="current" class="schedule-bound">
                <div>
                  <dt>{{ t('scout.schedule.bound') }}</dt>
                  <dd>
                    {{ planName }}
                    <details>
                      <summary>{{ t('scout.schedule.details') }}</summary>
                      <code>{{ current.plan_revision_public_id }}</code
                      ><code>{{ current.plan_hash }}</code>
                    </details>
                  </dd>
                </div>
              </dl>
            </section></FormCard
          >
          <FormCard
            ><aside class="schedule-card">
              <h3>{{ t('scout.schedule.next') }}</h3>
              <dl v-if="current" class="schedule-next">
                <div>
                  <dt class="visually-hidden">{{ t('scout.schedule.next') }}</dt>
                  <dd>
                    {{
                      current.enabled
                        ? current.next_scheduled_at
                          ? moment(current.next_scheduled_at) + ' · ' + current.timezone
                          : t('scout.schedule.unknownNext')
                        : t('scout.schedule.noNext')
                    }}
                  </dd>
                </div>
              </dl>
              <p v-if="current" class="small scout-muted">
                {{ t('scout.schedule.version', { number: current.version }) }}
              </p>
              <p v-if="differentPlan" class="alert alert-info">
                {{ t('scout.schedule.differentPlan') }}
              </p>
              <p class="small scout-muted">{{ t('scout.schedule.rules') }}</p>
              <p class="small scout-muted">
                {{ t('scout.schedule.offline') }} {{ t('scout.schedule.window') }}
              </p>
              <div class="scout-schedule-buttons mt-3">
                <button
                  type="button"
                  class="btn btn-outline-secondary"
                  :disabled="props.disabled || writing || confirming || state === 'loading'"
                  @click="reload"
                >
                  {{ t('scout.schedule.reload') }}</button
                ><button
                  v-if="current?.enabled"
                  type="button"
                  class="btn btn-outline-secondary"
                  :disabled="props.disabled || confirming || !canWrite"
                  @click="write({ disableOnly: true })"
                >
                  {{ t('scout.schedule.disable') }}
                </button>
              </div>
              <p v-if="current" class="small scout-muted">{{ t('scout.schedule.disableHint') }}</p>
            </aside></FormCard
          >
        </div>
        <section
          v-if="current"
          class="scout-schedule-history schedule-card"
          :aria-label="t('scout.schedule.history')"
        >
          <h3 class="h6">{{ t('scout.schedule.history') }}</h3>
          <p class="small scout-muted">{{ t('scout.schedule.historyHint') }}</p>
          <button
            type="button"
            class="btn btn-outline-secondary btn-sm"
            :disabled="historyLoading || writing"
            @click="readHistory()"
          >
            {{ t('scout.schedule.reloadHistory') }}
          </button>
          <p v-if="historyError" class="alert alert-warning mt-2" role="alert">
            {{ t('scout.schedule.errors.historyFailed') }}
          </p>
          <p v-if="historyLoading" role="status">{{ t('scout.schedule.historyLoading') }}</p>
          <p v-else-if="!history.length && !historyError" class="scout-muted">
            {{ t('scout.schedule.emptyHistory') }}
          </p>
          <div v-if="history.length" class="schedule-table-head" aria-hidden="true">
            <span>{{ t('scout.schedule.localDate') }}</span>
            <span>{{ t('scout.schedule.scheduledAt') }}</span>
            <span>{{ t('scout.schedule.status') }}</span>
            <span>{{ t('scout.schedule.details') }}</span>
          </div>
          <ol class="scout-schedule-days">
            <li v-for="day in history" :key="day.public_id">
              <strong>{{ scheduleDate(day.local_date, locale) }}</strong>
              <dl class="schedule-when">
                <dt class="visually-hidden">{{ t('scout.schedule.scheduledAt') }} (UTC)</dt>
                <dd>{{ scheduleMoment(day.scheduled_at, 'UTC', locale) }} UTC</dd>
              </dl>
              <span
                class="schedule-pill"
                :data-tone="
                  day.status === 'completed'
                    ? 'success'
                    : day.status === 'skipped'
                      ? 'warning'
                      : 'neutral'
                "
              >
                {{
                  t(
                    OCCURRENCE_STATUSES.includes(day.status)
                      ? `scout.schedule.statuses.${day.status}`
                      : 'scout.schedule.unknownStatus',
                  )
                }}
              </span>
              <div class="schedule-day-outcome">
                <p v-if="day.reason">
                  {{
                    t(
                      OCCURRENCE_REASONS.includes(day.reason)
                        ? `scout.schedule.reasons.${day.reason}`
                        : 'scout.schedule.unknownReason',
                    )
                  }}
                </p>
                <RouterLink
                  v-if="day.run_public_id"
                  :to="{
                    path: '/scout',
                    query: { job: jobId, run: day.run_public_id, mode: 'manual' },
                  }"
                  class="btn btn-outline-secondary btn-sm"
                  >{{ t('scout.schedule.openRun') }}</RouterLink
                >
                <details>
                  <summary>{{ t('scout.schedule.historyVersion') }}</summary>
                  <p>{{ t('scout.schedule.version', { number: day.schedule_version }) }}</p>
                  <p>
                    {{ t('scout.schedule.processedAt') }} ·
                    {{ scheduleMoment(day.processed_at, 'UTC', locale) }} UTC
                  </p>
                </details>
              </div>
            </li>
          </ol>
          <button
            v-if="historyCursor"
            type="button"
            class="btn btn-outline-secondary"
            :disabled="historyLoading || writing"
            @click="readHistory(true)"
          >
            {{ t('scout.schedule.historyMore') }}
          </button>
        </section>
        <div class="schedule-footer">
          <div class="scout-schedule-buttons">
            <button
              :form="`${uid}-schedule-form`"
              type="submit"
              class="btn btn-primary"
              :disabled="!canSave"
            >
              {{
                t(
                  writing
                    ? 'scout.schedule.saving'
                    : current
                      ? 'scout.schedule.save'
                      : 'scout.schedule.create',
                )
              }}</button
            ><button
              v-if="differentPlan && candidateReady"
              type="button"
              class="btn btn-outline-secondary"
              :disabled="!canRebind"
              @click="save(true)"
            >
              {{ t('scout.schedule.rebind') }}
            </button>
          </div>
        </div>
      </div>
    </details>
  </div>
</template>

<style scoped lang="scss">
.scout-schedule {
  min-width: 0;
  .scout-schedule-summary,
  .schedule-card,
  .schedule-footer {
    border: 1px solid var(--ui-border);
    border-radius: var(--ui-radius-lg);
    background: var(--ui-surface);
  }
  .scout-schedule-summary {
    padding: 1rem 1.25rem;
  }
  .scout-schedule-content {
    padding: 1rem 0 0;
  }
  .schedule-grid {
    display: grid;
    grid-template-columns: minmax(0, 1.7fr) minmax(0, 1fr);
    gap: 1rem;
    align-items: start;
  }
  .schedule-grid > :deep(.form-card) {
    border: 0;
    border-radius: var(--ui-radius-lg);
  }
  .schedule-card {
    min-width: 0;
    padding: 1.25rem;
  }
  .schedule-card h3 {
    font-size: 1rem;
    line-height: 1.4;
    margin: 0 0 1rem;
    font-weight: 600;
  }
  .schedule-card p {
    font-size: 0.8125rem;
  }
  .scout-schedule-fields {
    gap: 1rem;
  }
  .scout-schedule-fields :deep(.form-field) {
    display: block;
    border: 0;
    padding: 0;
  }
  .scout-schedule-fields :deep(.form-field__label) {
    font-size: 0.8125rem;
    margin-bottom: 0.375rem;
    font-weight: 600;
    color: var(--ui-text);
  }
  .scout-schedule-fields :deep(.form-control) {
    min-height: 44px;
    border-radius: var(--ui-radius);
  }
  .schedule-bound {
    margin: 1rem 0 0;
    padding-top: 1rem;
    border-top: 1px solid var(--ui-border);
    font-size: 0.8125rem;
  }
  .schedule-bound dt {
    color: var(--ui-text-muted);
  }
  .schedule-next dd {
    font-size: 1.25rem;
    font-weight: 600;
  }
  .schedule-pill {
    display: inline-flex;
    justify-self: start;
    border-radius: var(--ui-pill);
    background: var(--ui-surface-2);
    color: var(--ui-text);
    padding: 0.25rem 0.6rem;
    font-size: 0.75rem;
  }
  .schedule-pill[data-tone='success'] {
    background: var(--ui-success-soft);
    color: var(--bs-success-text-emphasis);
  }
  .schedule-pill[data-tone='warning'] {
    background: var(--ui-warning-soft);
    color: var(--bs-warning-text-emphasis);
  }
  .schedule-table-head,
  .scout-schedule-days > li {
    display: grid;
    grid-template-columns: 1fr 1fr 1fr 1.5fr;
    gap: 0.75rem;
    align-items: center;
  }
  .schedule-table-head {
    margin-top: 0.875rem;
    padding: 0.75rem;
    background: var(--ui-surface-2);
    color: var(--ui-text-muted);
    border: 1px solid var(--ui-border);
    border-radius: var(--ui-radius) var(--ui-radius) 0 0;
    font-size: 0.75rem;
    font-weight: 600;
  }
  .scout-schedule-days {
    margin: 0;
  }
  .scout-schedule-days > li {
    padding: 0.75rem;
    border: 1px solid var(--ui-border);
    border-top: 0;
    font-size: 0.8125rem;
  }
  .scout-schedule-days > li:last-child {
    border-radius: 0 0 var(--ui-radius) var(--ui-radius);
  }
  .schedule-day-outcome {
    min-width: 0;
    overflow-wrap: anywhere;
  }
  .schedule-when,
  .schedule-when dd,
  .schedule-day-outcome p {
    margin: 0;
  }
  .schedule-footer {
    margin-top: 1rem;
    padding: 0.75rem;
  }
  .schedule-footer .scout-schedule-buttons {
    margin: 0;
    justify-content: flex-end;
  }
  @media (max-width: 780px) {
    .schedule-grid {
      grid-template-columns: minmax(0, 1fr);
    }
    .schedule-table-head {
      display: none;
    }
    .scout-schedule-days > li {
      grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
      border-top: 1px solid var(--ui-border);
      border-radius: var(--ui-radius);
      margin-top: 0.5rem;
    }
  }
}
</style>
