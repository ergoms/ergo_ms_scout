<script setup>
import { computed, ref } from "vue";
import { useRouter } from "vue-router";
import { confirmAction } from "@/js/utils/confirm.js";
import { ArrowLeft, ArrowRight, Download, Info, Settings2 } from "@lucide/vue";
import SelectBox from "@/components/SelectBox.vue";
import SpinnerLoading from "@/components/SpinnerLoading.vue";
import ScoutStepRail from "./ScoutStepRail.vue";
import ScoutWorkspaceHeader from "./ScoutWorkspaceHeader.vue";
import { useThemeMode } from "@/composables/useThemeMode.js";
import { useAppI18n } from "@/i18n/useAppI18n.js";
import { applyThemeModePreference } from "@/js/theme-manager.js";
import { useScoutWorkflow } from "../js/useScoutWorkflow.js";
import { WORKFLOW_STEPS } from "../js/workflowState.js";
import ScoutModePicker from "./ScoutModePicker.vue";
import ScoutPlanSummary from "./ScoutPlanSummary.vue";
import ScoutRecordsTable from "./ScoutRecordsTable.vue";
import ScoutRuleEditor from "./ScoutRuleEditor.vue";
import ScoutPlanOptions from "./ScoutPlanOptions.vue";
import ScoutAiEditor from "./ScoutAiEditor.vue";
import ScoutVisualEditor from "./ScoutVisualEditor.vue";
import ScoutSchedulePanel from "./ScoutSchedulePanel.vue";
import ScoutCrawlSummary from "./ScoutCrawlSummary.vue";
import ScoutRunMetrics from "./ScoutRunMetrics.vue";
import "../styles/scout-results.scss";

defineProps({
  inspectorPrototype: Boolean,
  inspectorEnabled: { type: Boolean, default: true },
});
const { t } = useAppI18n();
const {
  form,
  scheduleJobId,
  schedulePlan,
  applySuggestion,
  recordsLoading,
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
} = useScoutWorkflow();
const router = useRouter(),
  schedulePanel = ref(null),
  navigationConfirm = ref(false),
  visualPresentation = ref(null),
  aiProposal = ref(false);
const navigationBlocked = computed(
  () =>
    loading.value ||
    busy.value ||
    navigationConfirm.value ||
    schedulePanel.value?.writing ||
    schedulePanel.value?.confirming,
);
async function openSection(section) {
  if (navigationBlocked.value) return;
  const target =
    section === "storage"
      ? {
          path: "/scout/storage",
          query: {
            ...(scheduleJobId.value ? { job: scheduleJobId.value } : {}),
            ...(scheduleJobId.value && run.value?.public_id
              ? { run: run.value.public_id }
              : {}),
          },
        }
      : {
          path: "/scout/compare",
          query:
            stage.value === "result" && run.value?.public_id
              ? { target: run.value.public_id }
              : {},
        };
  if (stage.value === "source" || dirty.value || schedulePanel.value?.dirty) {
    navigationConfirm.value = true;
    let yes;
    try {
      yes = await confirmAction({
        title: t(
          section === "storage"
            ? "scout.retention.leaveTitle"
            : "scout.design.leaveTitle",
        ),
        message: t(
          section === "storage"
            ? "scout.retention.leaveMessage"
            : "scout.design.leaveMessage",
        ),
        confirmText: t(
          section === "storage"
            ? "scout.retention.leaveConfirm"
            : "scout.design.leaveConfirm",
        ),
        cancelText: t("scout.retention.cancel"),
        variant: "warning",
      });
    } finally {
      navigationConfirm.value = false;
    }
    if (!yes) return;
  }
  await router.push(target);
}
const { preference } = useThemeMode();
const selectedTheme = computed({
  get: () => preference.value,
  set: applyThemeModePreference,
});
const themeOptions = computed(() =>
  ["light", "dark", "auto"].map((id) => ({
    id,
    name: t(`scout.workspace.themes.${id}`),
  })),
);
const exportOptions = computed(() =>
  ["json", "csv"].map((id) => ({ id, name: id.toUpperCase() })),
);
const steps = computed(() => {
  const current = WORKFLOW_STEPS.indexOf(stage.value);
  return WORKFLOW_STEPS.map((id, index) => ({
    id,
    marker: String(index + 1),
    label: t(`scout.workspace.steps.${id}`),
    state:
      index < current ? "passed" : index === current ? "current" : "upcoming",
    disabled: !canVisit(id),
  }));
});
const shownRecords = computed(() =>
  stage.value === "collect" ? records.value.slice(-20) : records.value,
);
const shownPreview = computed(() => previewRows.value.slice(0, 20));
const activeStatus = computed(() => run.value?.status);
const previewPending = computed(
  () => activeStatus.value === "exploring" || Boolean(returning.value),
);
const stalePreview = computed(
  () =>
    dirty.value ||
    (activeStatus.value === "awaiting_confirmation" && !planReady.value),
);
const header = computed(() => {
  const subtitle =
    form.value.title || t(`scout.workspace.leads.${stage.value}`);
  if (stage.value === "mode") {
    return {
      title: t("scout.newJob"),
      subtitle: t("scout.design.chooseMode"),
      status: t("scout.design.draft"),
    };
  }
  if (stage.value === "source") {
    if (mode.value === "visual") {
      return {
        title: t("scout.design.visual"),
        subtitle,
        ...visualPresentation.value,
      };
    }
    if (mode.value === "ai") {
      return {
        title: t(
          aiProposal.value ? "scout.design.aiProposal" : "scout.design.ai",
        ),
        subtitle: t("scout.design.aiHint"),
        status: aiProposal.value ? t("scout.design.aiReady") : "",
        tone: "success",
      };
    }
    return {
      title: t("scout.workspace.steps.source"),
      subtitle,
      status: t("scout.design.manual"),
      tone: "info",
    };
  }
  if (stage.value === "preview") {
    const title = returning.value
      ? "scout.flow.cancellingLead"
      : previewPending.value
        ? "scout.flow.preparing"
        : stalePreview.value
          ? "scout.design.refreshPreview"
          : "scout.flow.previewReady";
    return { title: t(title), subtitle };
  }
  if (stage.value === "confirm")
    return { title: t("scout.workspace.confirmPlan"), subtitle };
  if (stage.value === "collect") {
    const title =
      activeStatus.value === "running"
        ? "scout.design.running"
        : activeStatus.value === "paused"
          ? "scout.design.paused"
          : `scout.statuses.${activeStatus.value}`;
    return { title: t(title), subtitle };
  }
  const title =
    activeStatus.value === "completed"
      ? "scout.design.savedResult"
      : activeStatus.value === "completed_with_errors"
        ? "scout.design.partial"
        : `scout.statuses.${activeStatus.value || "completed"}`;
  return { title: t(title), subtitle };
});
const journal = computed(() => {
  const lines = run.value?.journal || [];
  return [
    ...lines,
    ...(run.value?.events || []).filter((line) => !lines.includes(line)),
  ];
});
const gaps = computed(() =>
  shownPreview.value.reduce(
    (count, row) =>
      count +
      Object.values(row.values || {}).filter(
        (cell) =>
          !cell?.images?.length && (cell?.normalized ?? cell?.raw ?? "") === "",
      ).length,
    0,
  ),
);
const previewMetrics = computed(() => [
  { label: t("scout.design.previewRows"), value: shownPreview.value.length },
  {
    label: t("scout.design.previewFields"),
    value: (confirmationPlan.value?.fields || payload().fields).length,
  },
  { label: t("scout.design.previewGaps"), value: gaps.value },
  { label: t("scout.design.downloaded"), value: 0 },
]);
const collectionMetrics = computed(() =>
  ["fetched", "extracted", "records", "errors"].map((key) => ({
    label: t(`scout.workspace.metrics.${key}`),
    value: progress.value[key],
  })),
);
const resultMetrics = computed(() => {
  const total = run.value?.record_count ?? records.value.length;
  const complete =
    !recordsLoading.value &&
    !recordsError.value &&
    records.value.length === total;
  const saved = new Set();
  const failed = new Set();
  let missing = 0;
  if (complete) {
    for (const row of records.value) {
      let hasMissingImage = false;
      for (const value of Object.values(row.values || {})) {
        if (!Array.isArray(value?.images)) continue;
        if (!value.images.length) hasMissingImage = true;
        for (const image of value.images) {
          if (image.status === "saved" && image.public_id)
            saved.add(image.public_id);
          if (image.status === "failed" && image.source_url)
            failed.add(image.source_url);
        }
      }
      if (hasMissingImage) missing++;
    }
  }
  return [
    { label: t("scout.design.savedRows"), value: total },
    {
      label: t("scout.design.savedImages"),
      value: complete ? saved.size : "—",
    },
    {
      label: t("scout.design.failedImages"),
      value: complete ? failed.size : "—",
    },
    { label: t("scout.design.missingImages"), value: complete ? missing : "—" },
  ];
});
const pipeline = computed(() => [
  { id: "fetch", value: progress.value.fetched },
  {
    id: "crawl",
    value: run.value?.frontier
      ? t("scout.workspace.foundUrls", {
          count: Object.values(run.value.frontier).reduce(
            (sum, n) => sum + (Number(n) || 0),
            0,
          ),
        })
      : t("scout.workspace.notReported"),
  },
  { id: "extract", value: progress.value.extracted },
  { id: "records", value: progress.value.records },
]);
function canVisit(id) {
  if (loading.value || returning.value) return false;
  if (!run.value)
    return (
      ["mode", "source"].includes(id) &&
      (id === "mode" || stage.value === "source")
    );
  if (run.value.status === "awaiting_confirmation")
    return id === "preview" || (id === "confirm" && canConfirm.value);
  return id === stage.value;
}
function visitStep(id) {
  if (!canVisit(id)) return;
  if (id === "mode" || id === "source") editorStep.value = id;
  if (id === "preview") confirmationOpen.value = false;
  if (id === "confirm") confirmationOpen.value = true;
}
function resetMode() {
  visualPresentation.value = null;
  aiProposal.value = false;
  editorStep.value = "mode";
}
function openSystemSettings() {
  window.dispatchEvent(
    new CustomEvent("ergo:open-user-settings", { detail: { tab: "system" } }),
  );
}
</script>

<template>
  <div class="scout-workflow scout-design container-fluid" :aria-busy="loading">
    <ScoutWorkspaceHeader
      v-bind="header"
      :locked="navigationBlocked"
      @navigate="openSection"
    >
      <template #actions>
        <SelectBox
          id="scout-theme"
          v-model="selectedTheme"
          :options="themeOptions"
          value-key="id"
          label-key="name"
          :include-all-option="false"
          :aria-label="t('scout.workspace.theme')"
        />
        <button
          type="button"
          class="btn btn-outline-secondary scout-system-button"
          :title="t('scout.workspace.systemSettings')"
          :aria-label="t('scout.workspace.systemSettings')"
          @click="openSystemSettings"
        >
          <Settings2 :size="18" aria-hidden="true" />
        </button>
        <button
          v-if="stage !== 'mode'"
          type="button"
          class="btn btn-outline-secondary"
          :disabled="loading || !!returning"
          @click="newJob"
        >
          {{ t("scout.newJob") }}
        </button>
      </template>
    </ScoutWorkspaceHeader>
    <ScoutStepRail
      :steps="steps"
      interactive
      :aria-label="t('scout.workspace.stepsLabel')"
      class="scout-step-rail"
      @select="visitStep"
    />
    <div v-if="inspectorPrototype" class="alert alert-info" role="note">
      {{ t("scout.inspector.prototype") }}
    </div>
    <div v-if="pageError" class="alert alert-danger" role="alert">
      {{ pageError }}
    </div>
    <div
      v-if="connectionLost"
      class="alert alert-warning scout-alert-actions"
      role="alert"
    >
      <div>{{ t("scout.workspace.connectionLost") }}</div>
      <button
        type="button"
        class="btn btn-outline-secondary btn-sm"
        :disabled="loading"
        @click="reload"
      >
        {{ t("scout.workspace.retry") }}
      </button>
    </div>

    <div class="scout-workspace">
      <div class="scout-main">
        <template v-if="stage === 'mode'">
          <ScoutModePicker
            v-model="mode"
            :disabled="loading"
            @open="editorStep = 'source'"
          />
          <aside class="scout-design-note">
            <Info :size="18" aria-hidden="true" />
            <div>
              <strong>{{ t("scout.design.startsAfterReview") }}</strong>
              <p>{{ t("scout.workspace.previewOnly") }}</p>
            </div>
          </aside>
        </template>

        <section
          v-else-if="stage === 'source'"
          class="scout-source-stage"
          :aria-label="t('scout.workspace.steps.source')"
        >
          <ScoutVisualEditor
            v-if="mode === 'visual'"
            v-model="form"
            :inspector-prototype="inspectorPrototype || inspectorEnabled"
            :disabled="loading"
            @valid="visualPlanValid = $event"
            @preview="checkPage"
            @manual="mode = 'manual'"
            @back="resetMode"
            @presentation="visualPresentation = $event"
          />
          <template v-else-if="mode === 'manual'">
            <ScoutRuleEditor
              v-model="form"
              :errors="errors"
              :disabled="loading"
              @add-field="addField"
              @remove-field="removeField"
            >
              <template #advanced
                ><ScoutPlanOptions v-model="form" :disabled="loading"
              /></template>
            </ScoutRuleEditor>
            <div class="scout-action-dock">
              <button
                type="button"
                class="btn btn-outline-secondary"
                :disabled="loading"
                @click="resetMode"
              >
                {{ t("scout.workspace.changeMode") }}
              </button>
              <button
                type="button"
                class="btn btn-primary"
                :disabled="loading"
                @click="checkPage"
              >
                {{
                  loading
                    ? t("scout.flow.preparing")
                    : t("scout.workspace.preparePreview")
                }}
                <ArrowRight :size="17" aria-hidden="true" />
              </button>
            </div>
          </template>
          <ScoutAiEditor
            v-else
            v-model="form"
            :disabled="loading"
            @apply="applySuggestion"
            @manual="mode = 'manual'"
            @back="resetMode"
            @proposal="aiProposal = $event"
          />
          <ScoutPlanOptions
            v-if="mode !== 'manual'"
            v-model="form"
            :disabled="loading"
          />
        </section>

        <template v-else-if="stage === 'preview'">
          <div v-if="previewPending" class="scout-card" role="status">
            <SpinnerLoading />
            <p class="scout-muted mt-3">
              {{
                returning
                  ? t("scout.workspace.returning")
                  : t("scout.flow.preparingLead")
              }}
            </p>
          </div>
          <template v-else>
            <ScoutRunMetrics
              v-if="shownPreview.length"
              :items="previewMetrics"
            />
            <div
              v-if="previewError || run?.error_summary"
              class="alert alert-warning mb-0"
              role="alert"
            >
              {{ previewError || run.error_summary }}
            </div>
            <p v-if="gaps" class="alert alert-warning mb-0" role="status">
              {{ t("scout.flow.gaps", { count: gaps }) }}
            </p>
            <section class="scout-card">
              <header class="scout-panel-heading">
                <h2>{{ t("scout.design.sample") }}</h2>
              </header>
              <p v-if="!previewRows.length" class="scout-muted" role="status">
                {{ t("scout.flow.previewEmpty") }}
              </p>
              <template v-else>
                <p class="scout-muted small">
                  {{
                    t("scout.flow.sample", {
                      shown: shownPreview.length,
                      total: previewRows.length,
                    })
                  }}
                </p>
                <ScoutRecordsTable :records="shownPreview" preview />
              </template>
            </section>
            <p
              v-if="stalePreview"
              class="alert alert-warning mb-0"
              role="status"
            >
              {{ t("scout.workspace.planChanged") }}
            </p>
          </template>
          <div class="scout-action-dock">
            <button
              type="button"
              class="btn btn-outline-secondary"
              :disabled="busy || !!returning"
              @click="changeRules"
            >
              <ArrowLeft :size="17" aria-hidden="true" />{{
                t("scout.workspace.editRules")
              }}
            </button>
            <button
              v-if="
                run?.status === 'awaiting_confirmation' && previewRows.length
              "
              type="button"
              class="btn btn-primary"
              :disabled="!canConfirm"
              @click="confirmationOpen = true"
            >
              {{ t("scout.workspace.toConfirmation")
              }}<ArrowRight :size="17" aria-hidden="true" />
            </button>
          </div>
        </template>

        <template v-else-if="stage === 'confirm'">
          <ScoutPlanSummary
            v-if="confirmationPlan"
            :plan="confirmationPlan"
            :mode="mode"
            expanded
            :approved="!!run?.approved"
          />
          <aside class="scout-design-note">
            <Info :size="18" aria-hidden="true" />
            <div>{{ t("scout.workspace.confirmHint") }}</div>
          </aside>
          <p
            v-if="run?.approved"
            class="alert alert-warning mb-0"
            role="status"
          >
            {{ t("scout.flow.startHeld") }}
          </p>
          <p v-if="!planReady" class="alert alert-warning mb-0" role="status">
            {{ t("scout.workspace.planChanged") }}
          </p>
          <div class="scout-action-dock">
            <button
              type="button"
              class="btn btn-outline-secondary"
              :disabled="loading"
              @click="confirmationOpen = false"
            >
              <ArrowLeft :size="17" aria-hidden="true" />{{
                t("scout.workspace.toPreview")
              }}
            </button>
            <button
              type="button"
              class="btn btn-primary"
              :disabled="!canConfirm || connectionLost"
              @click="confirmAndStart"
            >
              {{
                run?.approved
                  ? t("scout.start")
                  : t("scout.workspace.confirmAndRun")
              }}
            </button>
          </div>
        </template>

        <template v-else-if="stage === 'collect'">
          <ScoutRunMetrics :items="collectionMetrics" />
          <section class="scout-card">
            <header class="scout-panel-heading">
              <h2>{{ t("scout.workspace.processingOrder") }}</h2>
            </header>
            <p class="scout-muted" role="status">
              {{ t(`scout.workspace.execution.${run?.status || "running"}`) }}
            </p>
            <div class="table-responsive">
              <table class="table scout-process-table">
                <thead>
                  <tr>
                    <th scope="col">{{ t("scout.design.processStep") }}</th>
                    <th scope="col">
                      {{ t("scout.design.processDescription") }}
                    </th>
                    <th scope="col">{{ t("scout.design.processStatus") }}</th>
                  </tr>
                </thead>
                <tbody>
                  <tr v-for="item in pipeline" :key="item.id">
                    <th scope="row">
                      {{ t(`scout.workspace.pipeline.${item.id}.title`) }}
                    </th>
                    <td>
                      {{ t(`scout.workspace.pipeline.${item.id}.description`) }}
                    </td>
                    <td>
                      <span class="scout-chip" data-tone="info">{{
                        item.value
                      }}</span>
                    </td>
                  </tr>
                </tbody>
              </table>
            </div>
          </section>
          <section class="scout-card">
            <header class="scout-panel-heading">
              <h2>{{ t("scout.design.results") }}</h2>
            </header>
            <p v-if="!shownRecords.length" class="scout-muted" role="status">
              {{ t("scout.flow.waitingRows") }}
            </p>
            <template v-else>
              <p class="scout-muted small">
                {{
                  t("scout.flow.recent", {
                    shown: shownRecords.length,
                    total: progress.records,
                  })
                }}
              </p>
              <ScoutRecordsTable
                :records="shownRecords"
                :run-id="run.public_id"
                preview
              />
            </template>
          </section>
          <div
            v-if="['running', 'paused'].includes(run?.status)"
            class="scout-action-dock"
          >
            <button
              type="button"
              class="btn btn-outline-secondary"
              :disabled="busy || connectionLost"
              @click="cancelRun"
            >
              {{ t("scout.cancel") }}
            </button>
            <button
              v-if="run.status === 'running'"
              type="button"
              class="btn btn-primary"
              :disabled="busy || connectionLost"
              @click="pause"
            >
              {{ t("scout.pause") }}
            </button>
            <button
              v-else
              type="button"
              class="btn btn-primary"
              :disabled="busy || connectionLost"
              @click="resume"
            >
              {{ t("scout.resume") }}
            </button>
          </div>
        </template>

        <template v-else>
          <ScoutRunMetrics :items="resultMetrics" />
          <p
            v-if="run?.error_summary"
            class="alert alert-warning mb-0"
            role="alert"
          >
            {{ run.error_summary }}
          </p>
          <p
            v-if="run?.status === 'completed_with_errors' || run?.stop_reason"
            class="alert alert-warning mb-0"
          >
            {{ t("scout.workspace.partialResult") }}
          </p>
          <section class="scout-card">
            <header class="scout-panel-heading">
              <h2>{{ t("scout.design.results") }}</h2>
              <RouterLink
                v-if="
                  run &&
                  ['completed', 'completed_with_errors'].includes(run.status)
                "
                :to="{
                  path: '/scout/compare',
                  query: { target: run.public_id },
                }"
                class="btn btn-outline-secondary"
              >
                {{ t("scout.comparison.open") }}
              </RouterLink>
            </header>
            <p v-if="recordsLoading" role="status">
              {{ t("scout.product.loadingRecords") }}
            </p>
            <p
              v-if="!records.length && !recordsError && !recordsLoading"
              class="scout-muted"
            >
              {{ t("scout.flow.resultEmpty") }}
            </p>
            <ScoutRecordsTable
              v-if="records.length"
              :records="records"
              :run-id="run.public_id"
            />
            <div v-if="records.length" class="scout-result-tools">
              <span class="small scout-muted">{{
                t("scout.product.loaded", { count: records.length })
              }}</span>
              <SelectBox
                v-if="canExport"
                id="scout-export"
                v-model="exportKind"
                :options="exportOptions"
                value-key="id"
                label-key="name"
                :include-all-option="false"
                :disabled="exporting"
                :aria-label="t('scout.flow.exportKind')"
              />
            </div>
          </section>
          <div class="scout-action-dock">
            <button
              type="button"
              class="btn btn-outline-secondary"
              :disabled="loading"
              @click="changeRules"
            >
              {{ t("scout.workspace.editRules") }}
            </button>
            <button
              v-if="canExport"
              type="button"
              class="btn btn-primary"
              :disabled="exporting"
              @click="exportFile"
            >
              <Download :size="17" aria-hidden="true" />{{
                t("scout.flow.download")
              }}
            </button>
          </div>
        </template>

        <div
          v-if="recordsError"
          class="alert alert-warning scout-alert-actions mb-0"
          role="alert"
        >
          <span>{{ t("scout.workspace.recordsFailed") }}</span>
          <button
            type="button"
            class="btn btn-outline-secondary btn-sm"
            :disabled="loading"
            @click="reload"
          >
            {{ t("scout.workspace.retry") }}
          </button>
        </div>
        <ScoutSchedulePanel
          v-if="
            scheduleJobId && ['confirm', 'collect', 'result'].includes(stage)
          "
          ref="schedulePanel"
          :key="scheduleJobId"
          :job-id="scheduleJobId"
          :plan="schedulePlan"
          :plan-dirty="dirty"
          :disabled="loading"
        />
        <details
          v-if="run && (journal.length || run.crawl?.mode === 'site')"
          class="scout-execution-details"
          @toggle="stage === 'collect' && $event.target.open && reload()"
        >
          <summary>{{ t("scout.flow.details") }}</summary>
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
    </div>
  </div>
</template>
