<script setup>
import { computed, onUnmounted, ref, watch } from "vue";
import { apiClient } from "@/js/api/manager";
import { useAppI18n } from "@/i18n/useAppI18n.js";
import ScoutAiExample from "./ScoutAiExample.vue";
import ScoutPlanSummary from "./ScoutPlanSummary.vue";
const form = defineModel({ type: Object, required: true });
defineProps({ disabled: Boolean });
const emit = defineEmits(["apply", "manual", "back", "proposal"]);
const { t } = useAppI18n();
let nextKey = 1;
function newPage(url = "") {
  const key = nextKey++;
  return {
    key,
    url,
    snapshotId: "",
    snapshotPath: "",
    id: key === 1 ? "catalog" : `type_${key}`,
    action: "extract",
    pattern: "",
  };
}
const pages = ref([newPage(form.value.url)]);
const busy = ref(false),
  error = ref(""),
  suggestion = ref(null);
watch(suggestion, (value) => emit("proposal", Boolean(value)), {
  immediate: true,
});
let generation = 0,
  request = 0;
const stamp = computed(() =>
  JSON.stringify({
    pages: pages.value,
    goal: form.value.aiGoal,
    proxy: form.value.proxyRevision,
  }),
);
const patternError = computed(() => {
  if (pages.value.length === 1 || pages.value.some((page) => !page.snapshotId))
    return "";
  for (const page of pages.value) {
    if (
      !page.pattern.trim() ||
      !page.snapshotPath.startsWith(page.pattern.trim())
    )
      return t("scout.product.prefixMismatch", {
        id: page.id,
        path: page.snapshotPath,
      });
    const other = pages.value.find(
      (candidate) =>
        candidate !== page &&
        candidate.pattern.trim() &&
        page.snapshotPath.startsWith(candidate.pattern.trim()),
    );
    if (other)
      return t("scout.product.prefixOverlap", {
        path: page.snapshotPath,
        first: page.id,
        second: other.id,
      });
  }
  return "";
});
const canSuggest = computed(
  () =>
    Boolean(form.value.aiGoal.trim()) &&
    pages.value.every((page) => page.snapshotId) &&
    !patternError.value,
);
watch(
  stamp,
  () => {
    generation++;
    suggestion.value = null;
    error.value = "";
  },
  { flush: "sync" },
);
watch(
  () => form.value.proxyRevision,
  () => {
    pages.value.forEach((page) => {
      page.snapshotId = "";
    });
  },
  { flush: "sync" },
);
watch(
  () => pages.value[0]?.url,
  (url) => {
    form.value.url = url ?? "";
  },
);
onUnmounted(() => {
  generation++;
  request++;
});
async function suggest() {
  if (busy.value || !canSuggest.value) return;
  const owner = generation,
    token = ++request;
  busy.value = true;
  error.value = "";
  suggestion.value = null;
  const examples = pages.value.map((page) => ({
    snapshot_public_id: page.snapshotId,
    id: page.id,
    action: page.action,
    match: [{ kind: "prefix", pattern: page.pattern.trim() }],
  }));
  try {
    const response = await apiClient.post(
      "scout/plan-suggestions/",
      {
        goal: form.value.aiGoal.trim(),
        ...(examples.length === 1
          ? { snapshot_public_id: examples[0].snapshot_public_id }
          : { pages: examples }),
      },
      true,
      { quietStatuses: [400, 403, 404, 409, 422, 429, 500, 502, 503] },
    );
    if (owner !== generation || token !== request) return;
    if (response.success && response.data.draft?.fields?.length)
      suggestion.value = response.data.draft;
    else
      error.value =
        response.data?.detail || response.message || t("scout.product.failed");
  } catch {
    if (owner === generation && token === request)
      error.value = t("scout.product.failed");
  } finally {
    if (token === request) busy.value = false;
  }
}
</script>
<template>
  <section class="scout-ai-stage">
    <div v-show="!suggestion" class="scout-ai-stage">
      <div class="scout-two-pane">
        <section class="scout-card">
          <header class="scout-panel-heading">
            <h2>{{ t("scout.design.aiGoal") }}</h2>
          </header>
          <label class="scout-input-label" for="scout-goal">
            {{ t("scout.product.goal") }}
            <textarea
              id="scout-goal"
              v-model="form.aiGoal"
              class="form-control"
              rows="4"
              maxlength="2000"
              :disabled="disabled || busy"
            />
          </label>
        </section>
        <aside class="scout-card">
          <header class="scout-panel-heading">
            <h2>{{ t("scout.design.aiNext") }}</h2>
          </header>
          <ol class="scout-ai-steps">
            <li v-for="step in ['pages', 'proposal', 'preview']" :key="step">
              <strong>{{ t(`scout.design.aiSteps.${step}.title`) }}</strong>
              <p class="scout-muted">
                {{ t(`scout.design.aiSteps.${step}.detail`) }}
              </p>
            </li>
          </ol>
        </aside>
      </div>
      <section class="scout-card">
        <header class="scout-panel-heading">
          <div>
            <h2>{{ t("scout.product.sourcePages") }}</h2>
            <p class="scout-muted mb-0">{{ t("scout.design.aiExamples") }}</p>
          </div>
          <button
            type="button"
            class="btn btn-outline-secondary"
            :disabled="disabled || busy || pages.length >= 5"
            @click="pages.push(newPage())"
          >
            {{ t("scout.product.addPage") }}
          </button>
        </header>
        <ScoutAiExample
          v-for="(page, index) in pages"
          :key="page.key"
          v-model="pages[index]"
          :proxy-revision="form.proxyRevision"
          :disabled="disabled || busy"
          :multiple="pages.length > 1"
          @remove="pages.splice(index, 1)"
        />
        <p
          v-if="patternError"
          class="alert alert-warning mt-3 mb-0"
          role="alert"
        >
          {{ patternError }}
        </p>
        <p v-if="error" class="alert alert-warning mt-3 mb-0" role="alert">
          {{ error }}
        </p>
      </section>
      <div class="scout-action-dock">
        <div class="scout-ai-secondary">
          <button
            type="button"
            class="btn btn-outline-secondary"
            :disabled="disabled"
            @click="emit('back')"
          >
            {{ t("scout.design.outsideSelection") }}
          </button>
          <button
            type="button"
            class="btn btn-outline-secondary"
            :disabled="disabled || busy"
            @click="emit('manual')"
          >
            {{ t("scout.workspace.useManual") }}
          </button>
        </div>
        <button
          type="button"
          class="btn btn-primary"
          :disabled="disabled || busy || !canSuggest"
          @click="suggest"
        >
          {{ t(busy ? "scout.product.suggesting" : "scout.product.suggest") }}
        </button>
      </div>
    </div>
    <template v-if="suggestion">
      <aside class="scout-design-note">
        {{ t("scout.design.aiApplyHint") }}
      </aside>
      <ScoutPlanSummary :plan="suggestion" mode="ai" expanded />
      <div class="scout-action-dock">
        <button
          type="button"
          class="btn btn-outline-secondary"
          :disabled="disabled"
          @click="suggestion = null"
        >
          {{ t("scout.design.aiEdit") }}
        </button>
        <button
          type="button"
          class="btn btn-primary"
          :disabled="disabled"
          @click="emit('apply', suggestion)"
        >
          {{ t("scout.product.applySuggestion") }}
        </button>
      </div>
    </template>
  </section>
</template>
