<script setup>
import { useAppI18n } from "@/i18n/useAppI18n.js";

defineProps({
  section: { type: String, default: "workflow" },
  title: { type: String, required: true },
  subtitle: { type: String, default: "" },
  status: { type: String, default: "" },
  tone: { type: String, default: "neutral" },
  locked: Boolean,
});
defineEmits(["navigate"]);

const { t } = useAppI18n();
const tabs = [
  { id: "workflow", path: "/scout", label: "scout.design.nav.job" },
  { id: "compare", path: "/scout/compare", label: "scout.design.nav.compare" },
  {
    id: "storage",
    path: "/scout/storage",
    label: "scout.design.nav.storage",
    name: "scout.retention.title",
  },
];
</script>

<template>
  <header class="scout-design-header">
    <nav class="scout-design-tabs" :aria-label="t('scout.design.nav.label')">
      <template v-for="tab in tabs" :key="tab.id">
        <span
          v-if="section === tab.id"
          class="scout-design-tab"
          aria-current="page"
        >
          {{ t(tab.label) }}
        </span>
        <button
          v-else-if="section === 'workflow'"
          type="button"
          class="scout-design-tab"
          :aria-label="t(tab.name || tab.label)"
          :disabled="locked"
          @click="$emit('navigate', tab.id)"
        >
          {{ t(tab.label) }}
        </button>
        <RouterLink
          v-else
          :to="tab.path"
          class="scout-design-tab"
          :aria-label="t(tab.name || tab.label)"
        >
          {{ t(tab.label) }}
        </RouterLink>
      </template>
    </nav>
    <div class="scout-design-heading">
      <div class="scout-design-heading__copy">
        <h1>{{ title }}</h1>
        <p v-if="subtitle">{{ subtitle }}</p>
      </div>
      <div
        v-if="status || $slots.actions"
        class="scout-design-heading__actions"
      >
        <span
          v-if="status"
          class="scout-chip"
          :data-tone="tone"
          role="status"
          >{{ status }}</span
        >
        <slot name="actions" />
      </div>
    </div>
  </header>
</template>
