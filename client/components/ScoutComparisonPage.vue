<script setup>
import { computed, useId } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import FormCard from '@/components/FormCard.vue'
import FormField from '@/components/FormField.vue'
import { useAppI18n } from '@/i18n/useAppI18n.js'
import ScoutComparisonSide from './ScoutComparisonSide.vue'
import ScoutRunHistoryPicker from './ScoutRunHistoryPicker.vue'
import { useScoutComparison } from '../js/useScoutComparison.js'
import { COMPARISON_STATUSES } from '../js/comparisonState.js'
import { retentionCount } from '../js/retentionState.js'
import messages from '../js/comparisonLocales.js'
const route = useRoute(),
  router = useRouter(),
  { t, te, locale } = useAppI18n(),
  uid = useId()
const selection = computed(() => ({
  base: route.query.base,
  target: route.query.target,
  field: route.query.field,
}))
const {
  draft,
  ids,
  pair,
  job,
  fields,
  loading,
  pairError,
  result,
  changes,
  cursor,
  comparing,
  error,
  validLinks,
  canCompare,
  loadPair,
  compare,
  swap,
  selectHistory,
} = useScoutComparison(selection, (query) =>
  router.replace({ query: { ...route.query, ...query } }),
)
const summaries = computed(() =>
  result.value ? { base: result.value.base, target: result.value.target } : pair.value,
)
const back = computed(() => ({
  path: '/scout',
  query: {
    ...(pair.value?.target.job_public_id ? { job: pair.value.target.job_public_id } : {}),
    ...(ids.value.target ? { run: ids.value.target } : {}),
  },
}))
const count = (value) => retentionCount(value, locale.value)
function label(group, key) {
  return t(
    Object.hasOwn(messages.ru[group], key)
      ? `scout.comparison.${group}.${key}`
      : 'scout.comparison.unknown',
  )
}
function problemText(problem) {
  return label(
    'errors',
    Object.hasOwn(messages.ru.errors, problem.key) ? problem.key : 'requestFailed',
  )
}
function stopReasonLabel(reason) {
  const key = `scout.stopReasons.${reason}`
  return t(te(key) ? key : 'scout.comparison.unknown')
}
function resultRoute(side, storage = false) {
  const jobId = side.job_public_id || result.value?.job_public_id
  return {
    path: storage ? '/scout/storage' : '/scout',
    query: {
      ...(jobId ? { job: jobId } : {}),
      run: side.run_public_id || side.public_id,
    },
  }
}
function historyJobChanged(jobId) {
  router.replace({
    query: { ...route.query, job: jobId || undefined, base: undefined, target: undefined },
  })
}
</script>
<template>
  <main class="scout-workflow scout-comparison">
    <header class="scout-header">
      <div>
        <p class="scout-eyebrow">SCOUT</p>
        <h1>{{ t('scout.comparison.title') }}</h1>
        <p class="scout-muted">{{ t('scout.comparison.lead') }}</p>
      </div>
      <RouterLink :to="back" class="btn btn-outline-secondary">{{
        t('scout.comparison.back')
      }}</RouterLink>
    </header>
    <ScoutRunHistoryPicker
      :job-public-id="
        typeof route.query.job === 'string' ? route.query.job : pair?.target.job_public_id
      "
      :target-run-public-id="ids.target || undefined"
      :disabled="loading || comparing"
      @select="selectHistory"
      @job-change="historyJobChanged"
    />
    <FormCard class="scout-comparison-card"
      ><div class="scout-comparison-content">
        <p>{{ t('scout.comparison.manualHint') }}</p>
        <form @submit.prevent="loadPair">
          <fieldset :disabled="loading || comparing">
            <legend class="visually-hidden">{{ t('scout.comparison.title') }}</legend>
            <FormField :label="t('scout.comparison.baseLink')" :label-for="`${uid}-base`"
              ><input
                :id="`${uid}-base`"
                v-model="draft.base"
                class="form-control"
                type="text"
                inputmode="url"
                autocomplete="off"
                maxlength="4096"
                required /></FormField
            ><FormField :label="t('scout.comparison.targetLink')" :label-for="`${uid}-target`"
              ><input
                :id="`${uid}-target`"
                v-model="draft.target"
                class="form-control"
                type="text"
                inputmode="url"
                autocomplete="off"
                maxlength="4096"
                required
            /></FormField>
            <p class="small scout-muted">{{ t('scout.comparison.linkHint') }}</p>
            <p v-if="(draft.base || draft.target) && !validLinks" class="scout-warning">
              {{ t('scout.comparison.invalidLinks') }}
            </p>
            <div class="scout-comparison-buttons">
              <button type="submit" class="btn btn-primary" :disabled="!validLinks">
                {{ t('scout.comparison.check') }}</button
              ><button type="button" class="btn btn-outline-secondary" @click="swap">
                {{ t('scout.comparison.swap') }}
              </button>
            </div>
          </fieldset>
        </form>
        <p v-if="loading" role="status">{{ t('scout.comparison.loading') }}</p>
        <div v-if="pairError" class="alert alert-warning" role="alert">
          <p>{{ problemText(pairError) }}</p>
          <p v-if="pairError.detail" class="small mb-0">{{ pairError.detail }}</p>
        </div>
        <p v-if="job" class="mt-3">{{ job.title }}</p>
        <p class="small scout-muted">{{ t('scout.comparison.identityHint') }}</p>
        <form @submit.prevent="compare(false)">
          <FormField
            :label="t('scout.comparison.field')"
            :label-for="`${uid}-field`"
            :hint="t('scout.comparison.fieldHint')"
            ><input
              :id="`${uid}-field`"
              v-model="draft.field"
              class="form-control"
              type="text"
              :list="`${uid}-fields`"
              :disabled="loading || comparing"
              required /><datalist :id="`${uid}-fields`">
              <option v-for="field in fields" :key="field" :value="field" /></datalist
          ></FormField>
          <p class="small scout-muted">{{ t('scout.comparison.serverChecks') }}</p>
          <button type="submit" class="btn btn-primary" :disabled="!canCompare">
            {{ t('scout.comparison.compare') }}
          </button>
        </form>
      </div></FormCard
    >
    <div v-if="error" class="alert alert-warning mt-3" role="alert">
      <p>{{ problemText(error) }}</p>
      <p v-if="error.detail" class="small">{{ error.detail }}</p>
      <code v-if="error.run">{{ error.run }}</code>
      <div class="scout-comparison-buttons">
        <button
          type="button"
          class="btn btn-outline-secondary"
          :disabled="!canCompare"
          @click="compare(false)"
        >
          {{ t('scout.comparison.retry') }}</button
        ><button
          v-if="cursor"
          type="button"
          class="btn btn-outline-secondary"
          :disabled="!canCompare"
          @click="compare(true)"
        >
          {{ t('scout.comparison.retryPage') }}
        </button>
      </div>
    </div>
    <div v-if="summaries" class="scout-comparison-summaries">
      <FormCard v-for="(side, name) in summaries" :key="name" class="scout-comparison-card"
        ><section class="scout-comparison-content" :aria-label="t(`scout.comparison.${name}`)">
          <h2 class="h5">{{ t(`scout.comparison.${name}`) }}</h2>
          <p>{{ t(`scout.statuses.${side.status}`) }}</p>
          <p
            v-if="side.completeness"
            :class="{ 'scout-warning': side.completeness !== 'complete' }"
          >
            {{
              t(
                side.completeness === 'complete'
                  ? 'scout.comparison.complete'
                  : side.completeness === 'partial'
                    ? 'scout.comparison.partial'
                    : 'scout.comparison.unknownCompleteness',
              )
            }}
          </p>
          <dl class="scout-comparison-details">
            <div>
              <dt>{{ t('scout.comparison.recordCount') }}</dt>
              <dd>{{ count(side.record_count) }}</dd>
            </div>
            <div v-if="typeof side.failed_tasks === 'number'">
              <dt>{{ t('scout.comparison.failedTasks') }}</dt>
              <dd>{{ count(side.failed_tasks) }}</dd>
            </div>
            <div v-if="typeof side.unkeyed_records === 'number'">
              <dt>{{ t('scout.comparison.unkeyed') }}</dt>
              <dd>{{ count(side.unkeyed_records) }}</dd>
            </div>
            <div v-if="side.stop_reason">
              <dt>{{ t('scout.comparison.stopReason') }}</dt>
              <dd>
                {{ stopReasonLabel(side.stop_reason) }}
                <details>
                  <summary>{{ t('scout.comparison.technical') }}</summary>
                  <code>{{ side.stop_reason }}</code>
                </details>
              </dd>
            </div>
          </dl>
          <details>
            <summary>{{ t('scout.comparison.run') }}</summary>
            <code>{{ side.run_public_id || side.public_id }}</code>
          </details>
          <div class="scout-comparison-buttons">
            <RouterLink :to="resultRoute(side)" class="btn btn-outline-secondary">{{
              t('scout.comparison.openResult')
            }}</RouterLink
            ><RouterLink :to="resultRoute(side, true)" class="btn btn-outline-secondary">{{
              t('scout.comparison.protect')
            }}</RouterLink>
          </div>
        </section></FormCard
      >
    </div>
    <p v-if="comparing" class="mt-3" role="status">{{ t('scout.comparison.loading') }}</p>
    <template v-if="result"
      ><p
        v-if="result.base.completeness !== 'complete' || result.target.completeness !== 'complete'"
        class="alert alert-warning"
        role="status"
      >
        {{ t('scout.comparison.partialHint') }}
      </p>
      <p class="scout-muted">{{ t('scout.comparison.unkeyedHint') }}</p>
      <p class="scout-muted">{{ t('scout.comparison.protectHint') }}</p>
      <FormCard class="scout-comparison-card"
        ><section class="scout-comparison-content" :aria-labelledby="`${uid}-counts`">
          <h2 :id="`${uid}-counts`" class="h4">
            {{ t('scout.comparison.counts') }} · {{ result.field }}
          </h2>
          <dl class="scout-comparison-counts">
            <div v-for="status in COMPARISON_STATUSES" :key="status">
              <dt>{{ label('statuses', status) }}</dt>
              <dd>{{ count(result.counts[status]) }}</dd>
            </div>
          </dl>
          <p class="small scout-muted">{{ t('scout.comparison.unchangedHint') }}</p>
        </section></FormCard
      >
      <section :aria-labelledby="`${uid}-changes`">
        <h2 :id="`${uid}-changes`" class="h4">{{ t('scout.comparison.changes') }}</h2>
        <p>{{ t('scout.comparison.loaded', { count: count(changes.length) }) }}</p>
        <p v-if="!changes.length && !comparing">{{ t('scout.comparison.empty') }}</p>
        <ol class="scout-comparison-changes">
          <li v-for="item in changes" :key="item.entity_key">
            <FormCard
              ><article class="scout-comparison-content">
                <h3 class="h5">{{ item.sku || item.key?.value || t('scout.comparison.key') }}</h3>
                <p class="fw-semibold">{{ label('statuses', item.status) }}</p>
                <p class="small">{{ label('descriptions', item.status) }}</p>
                <details>
                  <summary>{{ t('scout.comparison.technical') }}</summary>
                  <code>{{ item.entity_key }}</code>
                </details>
                <div class="scout-comparison-values">
                  <ScoutComparisonSide
                    :side="item.before"
                    :title="t('scout.comparison.before')"
                  /><ScoutComparisonSide :side="item.after" :title="t('scout.comparison.after')" />
                </div></article
            ></FormCard>
          </li>
        </ol>
        <button
          v-if="cursor"
          type="button"
          class="btn btn-outline-secondary"
          :disabled="!canCompare"
          @click="compare(true)"
        >
          {{ t('scout.comparison.more') }}
        </button>
      </section>
    </template>
  </main>
</template>
