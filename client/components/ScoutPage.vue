<script setup>
import { computed, ref } from 'vue'
import { ArrowLeft, ArrowRight, Download, Settings2, Sparkles } from '@lucide/vue'
import FormCard from '@/components/FormCard.vue'
import FormField from '@/components/FormField.vue'
import SelectBox from '@/components/SelectBox.vue'
import SpinnerLoading from '@/components/SpinnerLoading.vue'
import ScoutStepRail from './ScoutStepRail.vue'
import { useThemeMode } from '@/composables/useThemeMode.js'
import { useAppI18n } from '@/i18n/useAppI18n.js'
import { applyThemeModePreference } from '@/js/theme-manager.js'
import { useScoutWorkflow } from '../js/useScoutWorkflow.js'
import { WORKFLOW_STEPS } from '../js/workflowState.js'
import ScoutModePicker from './ScoutModePicker.vue'
import ScoutPlanSummary from './ScoutPlanSummary.vue'
import ScoutRecordsTable from './ScoutRecordsTable.vue'
import ScoutRuleEditor from './ScoutRuleEditor.vue'
import ScoutVisualEditor from './ScoutVisualEditor.vue'
import ScoutCrawlSummary from './ScoutCrawlSummary.vue'

defineProps({ inspectorPrototype: Boolean })
const { t } = useAppI18n()
const {
  form,
  visualPlanValid,
  errors,
  run,
  records,
  mode,
  editorStep,
  confirmationOpen,
  returning,
  exportKind,
  loading,
  busy,
  exporting,
  connectionLost,
  recordsError,
  pageError,
  dirty,
  stage,
  planReady,
  confirmationPlan,
  canConfirm,
  canExport,
  previewRows,
  previewError,
  progress,
  payload,
  checkPage,
  changeRules,
  newJob,
  confirmAndStart,
  cancelRun,
  exportFile,
  reload,
  pause,
  resume,
  addField,
  removeField,
} = useScoutWorkflow()
const modelRequest = ref('')
const { preference } = useThemeMode()
const selectedTheme = computed({ get: () => preference.value, set: applyThemeModePreference })
const themeOptions = computed(() =>
  ['light', 'dark', 'auto'].map((id) => ({ id, name: t(`scout.workspace.themes.${id}`) })),
)
const exportOptions = computed(() => ['json', 'csv'].map((id) => ({ id, name: id.toUpperCase() })))
const steps = computed(() => {
  const current = WORKFLOW_STEPS.indexOf(stage.value)
  return WORKFLOW_STEPS.map((id, index) => ({
    id,
    marker: String(index + 1),
    label: t(`scout.workspace.steps.${id}`),
    state: index < current ? 'passed' : index === current ? 'current' : 'upcoming',
    disabled: !canVisit(id),
  }))
})
const shownRecords = computed(() =>
  stage.value === 'collect' ? records.value.slice(-20) : records.value,
)
const shownPreview = computed(() => previewRows.value.slice(0, 20))
const sidebarPlan = computed(() => (stage.value === 'confirm' ? confirmationPlan.value : payload()))
const journal = computed(() => {
  const lines = run.value?.journal || []
  return [...lines, ...(run.value?.events || []).filter((line) => !lines.includes(line))]
})
const gaps = computed(() =>
  shownPreview.value.reduce(
    (count, row) =>
      count +
      Object.values(row.values || {}).filter((cell) => (cell?.normalized ?? cell?.raw ?? '') === '')
        .length,
    0,
  ),
)
const pipeline = computed(() => [
  { id: 'fetch', value: progress.value.fetched },
  {
    id: 'crawl',
    value: run.value?.frontier
      ? t('scout.workspace.foundUrls', {
          count: Object.values(run.value.frontier).reduce((sum, n) => sum + (Number(n) || 0), 0),
        })
      : t('scout.workspace.notReported'),
  },
  { id: 'extract', value: progress.value.extracted },
  { id: 'records', value: progress.value.records },
])
function canVisit(id) {
  if (loading.value || returning.value) return false
  if (!run.value)
    return ['mode', 'source'].includes(id) && (id === 'mode' || stage.value === 'source')
  if (run.value.status === 'awaiting_confirmation')
    return id === 'preview' || (id === 'confirm' && canConfirm.value)
  return id === stage.value
}
function visitStep(id) {
  if (!canVisit(id)) return
  if (id === 'mode' || id === 'source') editorStep.value = id
  if (id === 'preview') confirmationOpen.value = false
  if (id === 'confirm') confirmationOpen.value = true
}
function openSystemSettings() {
  window.dispatchEvent(new CustomEvent('ergo:open-user-settings', { detail: { tab: 'system' } }))
}
</script>

<template>
  <div class="scout-workflow container-fluid" :aria-busy="loading">
    <div v-if="inspectorPrototype" class="alert alert-info" role="note">
      {{ t('scout.inspector.prototype') }}
    </div>
    <header class="scout-header">
      <div>
        <div class="scout-eyebrow">{{ t('scout.title') }}</div>
        <h1 class="h3 mb-1">
          {{ stage === 'mode' ? t('scout.newJob') : form.title || t('scout.workspace.job') }}
        </h1>
        <p class="scout-muted mb-0">{{ t(`scout.workspace.leads.${stage}`) }}</p>
      </div>
      <div class="scout-toolbar">
        <SelectBox
          id="scout-theme"
          v-model="selectedTheme"
          :options="themeOptions"
          value-key="id"
          label-key="name"
          :include-all-option="false"
          :aria-label="t('scout.workspace.theme')"
        />
        <button type="button" class="btn btn-outline-secondary" @click="openSystemSettings">
          <Settings2 :size="17" aria-hidden="true" /><span>{{
            t('scout.workspace.systemSettings')
          }}</span>
        </button>
        <button
          v-if="stage !== 'mode'"
          type="button"
          class="btn btn-outline-secondary"
          :disabled="loading || !!returning"
          @click="newJob"
        >
          {{ t('scout.newJob') }}
        </button>
      </div>
    </header>
    <ScoutStepRail
      :steps="steps"
      interactive
      :aria-label="t('scout.workspace.stepsLabel')"
      class="scout-step-rail"
      @select="visitStep"
    />
    <div v-if="pageError" class="alert alert-danger" role="alert">{{ pageError }}</div>
    <div v-if="connectionLost" class="alert alert-warning scout-alert-actions" role="alert">
      <div>{{ t('scout.workspace.connectionLost') }}</div>
      <button
        type="button"
        class="btn btn-outline-secondary btn-sm"
        :disabled="loading"
        @click="reload"
      >
        {{ t('scout.workspace.retry') }}
      </button>
    </div>
    <div
      class="scout-workspace"
      :class="{ 'scout-workspace--visual': stage === 'source' && mode === 'visual' }"
    >
      <div class="scout-main">
        <FormCard v-if="stage === 'mode'">
          <div class="scout-panel-content">
            <h2 class="h5">{{ t('scout.workspace.chooseMode') }}</h2>
            <p class="scout-muted">{{ t('scout.workspace.modeHint') }}</p>
            <ScoutModePicker v-model="mode" :disabled="loading" />
          </div>
          <template #footer
            ><button
              type="button"
              class="btn btn-primary"
              :disabled="loading"
              @click="editorStep = 'source'"
            >
              {{ t('scout.workspace.toSource')
              }}<ArrowRight :size="17" aria-hidden="true" /></button
          ></template>
        </FormCard>
        <section v-else-if="stage === 'source'" :aria-label="t('scout.workspace.steps.source')">
          <div class="scout-source-heading">
            <div>
              <span class="scout-muted small">{{ t('scout.workspace.modeLabel') }}</span>
              <div class="fw-semibold">{{ t(`scout.workspace.modes.${mode}.title`) }}</div>
            </div>
            <button
              type="button"
              class="btn btn-outline-secondary btn-sm"
              :disabled="loading"
              @click="editorStep = 'mode'"
            >
              {{ t('scout.workspace.changeMode') }}
            </button>
          </div>
          <template v-if="mode === 'manual'">
            <ScoutRuleEditor
              v-model="form"
              :errors="errors"
              :disabled="loading"
              @add-field="addField"
              @remove-field="removeField"
            />
            <div class="scout-actions">
              <span class="scout-muted small">{{ t('scout.workspace.previewOnly') }}</span
              ><button type="button" class="btn btn-primary" :disabled="loading" @click="checkPage">
                {{ loading ? t('scout.flow.preparing') : t('scout.workspace.preparePreview')
                }}<ArrowRight :size="17" aria-hidden="true" />
              </button>
            </div>
          </template>
          <ScoutVisualEditor
            v-else-if="mode === 'visual'"
            v-model="form"
            :inspector-prototype="inspectorPrototype"
            :disabled="loading"
            @valid="visualPlanValid = $event"
            @preview="checkPage"
            @manual="mode = 'manual'"
          />
          <FormCard v-else>
            <FormField :label="t('scout.url')" label-for="scout-mode-source"
              ><input id="scout-mode-source" v-model="form.url" class="form-control" type="url"
            /></FormField>
            <FormField
              v-if="mode === 'ai'"
              :label="t('scout.workspace.request')"
              label-for="scout-request"
              :hint="t('scout.workspace.requestHint')"
            >
              <textarea id="scout-request" v-model="modelRequest" class="form-control" rows="4" />
            </FormField>
            <div class="scout-panel-content scout-unavailable" role="status">
              <Sparkles :size="24" aria-hidden="true" />
              <h2 class="h5">{{ t('scout.workspace.notAvailable') }}</h2>
              <p>{{ t(`scout.workspace.modes.${mode}.unavailable`) }}</p>
              <p class="scout-muted small mb-0">{{ t('scout.workspace.draftPreserved') }}</p>
            </div>
            <template #footer
              ><button type="button" class="btn btn-primary" @click="mode = 'manual'">
                {{ t('scout.workspace.useManual')
                }}<ArrowRight :size="17" aria-hidden="true" /></button
            ></template>
          </FormCard>
        </section>
        <FormCard v-else-if="stage === 'preview'">
          <div class="scout-panel-content">
            <template v-if="run?.status === 'exploring' || returning"
              ><SpinnerLoading />
              <h2 class="h5 mt-3">
                {{ returning ? t('scout.flow.cancellingLead') : t('scout.flow.preparing') }}
              </h2>
              <p class="scout-muted mb-0">
                {{ returning ? t('scout.workspace.returning') : t('scout.flow.preparingLead') }}
              </p></template
            >
            <template v-else>
              <h2 class="h5">{{ t('scout.flow.previewReady') }}</h2>
              <p class="scout-muted">{{ t('scout.workspace.previewLead') }}</p>
              <div
                v-if="previewError || run?.error_summary"
                class="alert alert-warning"
                role="alert"
              >
                {{ previewError || run.error_summary }}
              </div>
              <p v-if="!previewRows.length" class="alert alert-info" role="status">
                {{ t('scout.flow.previewEmpty') }}
              </p>
              <template v-else
                ><p class="scout-muted small">
                  {{
                    t('scout.flow.sample', {
                      shown: shownPreview.length,
                      total: previewRows.length,
                    })
                  }}
                </p>
                <p v-if="gaps" class="scout-warning small">
                  {{ t('scout.flow.gaps', { count: gaps }) }}
                </p>
                <ScoutRecordsTable :records="shownPreview" preview
              /></template>
              <p
                v-if="dirty || (run?.status === 'awaiting_confirmation' && !planReady)"
                class="alert alert-warning mt-3"
                role="status"
              >
                {{ t('scout.workspace.planChanged') }}
              </p>
            </template>
          </div>
          <template #footer
            ><button
              type="button"
              class="btn btn-outline-secondary"
              :disabled="busy || !!returning"
              @click="changeRules"
            >
              <ArrowLeft :size="17" aria-hidden="true" />{{
                t('scout.workspace.editRules')
              }}</button
            ><button
              v-if="run?.status === 'awaiting_confirmation' && previewRows.length"
              type="button"
              class="btn btn-primary"
              :disabled="!canConfirm"
              @click="confirmationOpen = true"
            >
              {{ t('scout.workspace.toConfirmation')
              }}<ArrowRight :size="17" aria-hidden="true" /></button
          ></template>
        </FormCard>
        <FormCard v-else-if="stage === 'confirm'">
          <div class="scout-panel-content">
            <h2 class="h5">{{ t('scout.workspace.confirmPlan') }}</h2>
            <p class="scout-muted">{{ t('scout.workspace.beforeStart') }}</p>
            <ScoutPlanSummary
              v-if="confirmationPlan"
              :plan="confirmationPlan"
              :mode="mode"
              expanded
              :approved="!!run?.approved"
            />
            <div class="alert alert-info mt-4 mb-0">{{ t('scout.workspace.confirmHint') }}</div>
            <p v-if="run?.approved" class="scout-warning mt-3 mb-0" role="status">
              {{ t('scout.flow.startHeld') }}
            </p>
            <p v-if="!planReady" class="alert alert-warning mt-3 mb-0">
              {{ t('scout.workspace.planChanged') }}
            </p>
          </div>
          <template #footer
            ><button
              type="button"
              class="btn btn-outline-secondary"
              :disabled="loading"
              @click="confirmationOpen = false"
            >
              <ArrowLeft :size="17" aria-hidden="true" />{{
                t('scout.workspace.toPreview')
              }}</button
            ><button
              type="button"
              class="btn btn-primary"
              :disabled="!canConfirm || connectionLost"
              @click="confirmAndStart"
            >
              {{ run?.approved ? t('scout.start') : t('scout.workspace.confirmAndRun') }}
            </button></template
          >
        </FormCard>
        <FormCard v-else-if="stage === 'collect'">
          <div class="scout-panel-content">
            <h2 class="h5" aria-live="polite">{{ t(`scout.statuses.${run?.status}`) }}</h2>
            <p class="scout-muted">
              {{ t(`scout.workspace.execution.${run?.status || 'running'}`) }}
            </p>
            <div class="scout-stats">
              <div v-for="key in ['fetched', 'extracted', 'records', 'errors']" :key="key">
                <strong>{{ progress[key] }}</strong
                ><span>{{ t(`scout.workspace.metrics.${key}`) }}</span>
              </div>
            </div>
            <h3 class="h6">{{ t('scout.workspace.processingOrder') }}</h3>
            <ol class="scout-pipeline">
              <li v-for="(item, index) in pipeline" :key="item.id">
                <span class="scout-pipeline__number" aria-hidden="true">{{ index + 1 }}</span>
                <div>
                  <strong>{{ t(`scout.workspace.pipeline.${item.id}.title`) }}</strong>
                  <p class="scout-muted small mb-0">
                    {{ t(`scout.workspace.pipeline.${item.id}.description`) }}
                  </p>
                </div>
                <span class="scout-pipeline__value">{{ item.value }}</span>
              </li>
            </ol>
            <p class="scout-muted small">{{ t('scout.workspace.noPercentage') }}</p>
            <p v-if="!shownRecords.length" class="scout-muted" role="status">
              {{ t('scout.flow.waitingRows') }}
            </p>
            <template v-else
              ><h3 class="h6 mt-4">
                {{ t('scout.flow.recent', { shown: shownRecords.length, total: progress.records }) }}
              </h3>
              <ScoutRecordsTable :records="shownRecords" preview
            /></template>
          </div>
          <template #footer
            ><button
              v-if="run?.status === 'running'"
              type="button"
              class="btn btn-primary"
              :disabled="busy || connectionLost"
              @click="pause"
            >
              {{ t('scout.pause') }}</button
            ><button
              v-if="run?.status === 'paused'"
              type="button"
              class="btn btn-primary"
              :disabled="busy || connectionLost"
              @click="resume"
            >
              {{ t('scout.resume') }}</button
            ><button
              v-if="['running', 'paused'].includes(run?.status)"
              type="button"
              class="btn btn-outline-secondary"
              :disabled="busy || connectionLost"
              @click="cancelRun"
            >
              {{ t('scout.cancel') }}
            </button></template
          >
        </FormCard>
        <FormCard v-else>
          <div class="scout-panel-content">
            <h2 class="h5">{{ t(`scout.statuses.${run?.status || 'completed'}`) }}</h2>
            <p v-if="run?.error_summary" class="alert alert-warning" role="alert">
              {{ run.error_summary }}
            </p>
            <p
              v-if="run?.status === 'completed_with_errors' || run?.stop_reason"
              class="alert alert-warning"
            >
              {{ t('scout.workspace.partialResult') }}
            </p>
            <p class="scout-muted">
              {{ t('scout.flow.gotRecords', { records: run?.record_count || 0 }) }}
            </p>
            <p v-if="!records.length && !recordsError" class="scout-muted">
              {{ t('scout.flow.resultEmpty') }}
            </p>
            <ScoutRecordsTable v-if="records.length" :records="records" />
          </div>
          <template #footer
            ><button
              type="button"
              class="btn btn-outline-secondary"
              :disabled="loading"
              @click="changeRules"
            >
              {{ t('scout.workspace.editRules') }}
            </button>
            <div v-if="canExport" class="scout-export">
              <SelectBox
                id="scout-export"
                v-model="exportKind"
                :options="exportOptions"
                value-key="id"
                label-key="name"
                :include-all-option="false"
                :disabled="exporting"
                :aria-label="t('scout.flow.exportKind')"
              /><button
                type="button"
                class="btn btn-primary"
                :disabled="exporting"
                @click="exportFile"
              >
                <Download :size="17" aria-hidden="true" />{{ t('scout.flow.download') }}
              </button>
            </div></template
          >
        </FormCard>
        <div v-if="recordsError" class="alert alert-warning scout-alert-actions mt-3" role="alert">
          <span>{{ t('scout.workspace.recordsFailed') }}</span
          ><button
            type="button"
            class="btn btn-outline-secondary btn-sm"
            :disabled="loading"
            @click="reload"
          >
            {{ t('scout.workspace.retry') }}
          </button>
        </div>
        <details
          v-if="run && (journal.length || run.crawl?.mode === 'site')"
          class="scout-execution-details"
          @toggle="stage === 'collect' && $event.target.open && reload()"
        >
          <summary>{{ t('scout.flow.details') }}</summary>
          <ScoutCrawlSummary
            v-if="run.crawl?.mode === 'site'"
            :run="run"
            :depth="form.crawlDepth"
            :pages="form.crawlPages"
            :hosts="form.allowedHosts"
            :limits="form.crawlLimits"
            :visit="form.visitPrefix"
            :extract="form.extractPrefix"
          />
          <ul v-if="journal.length" class="scout-journal">
            <li v-for="(line, index) in journal" :key="index">{{ line }}</li>
          </ul>
        </details>
      </div>
      <aside class="scout-sidebar" :aria-label="t('scout.workspace.planTitle')">
        <template v-if="stage === 'mode'"
          ><h2 class="h6">{{ t('scout.workspace.commonPath') }}</h2>
          <ol class="scout-path">
            <li v-for="step in WORKFLOW_STEPS" :key="step">
              {{ t(`scout.workspace.steps.${step}`) }}
            </li>
          </ol></template
        >
        <template v-else-if="sidebarPlan">
          <h2 class="h6">{{ t('scout.workspace.planTitle') }}</h2>
          <ScoutPlanSummary :plan="sidebarPlan" :mode="mode" :approved="!!run?.approved" />
        </template>
        <p class="scout-sidebar-note">{{ t('scout.workspace.previewOnly') }}</p>
      </aside>
    </div>
  </div>
</template>
