<script setup>
import { CodeXml, MousePointer2, Sparkles } from "@lucide/vue";
import { useAppI18n } from "@/i18n/useAppI18n.js";

const mode = defineModel({ type: String, default: "manual" });
defineProps({ disabled: { type: Boolean, default: false } });
const emit = defineEmits(["open"]);
const { t } = useAppI18n();
const modes = [
  { id: "manual", icon: CodeXml },
  { id: "visual", icon: MousePointer2 },
  { id: "ai", icon: Sparkles },
];

function open(id) {
  mode.value = id;
  emit("open");
}
</script>

<template>
  <div class="scout-modes" :aria-label="t('scout.workspace.modeLabel')">
    <article
      v-for="item in modes"
      :key="item.id"
      class="scout-mode"
      :class="{ 'scout-mode--selected': mode === item.id }"
    >
      <div class="scout-mode__top">
        <span class="scout-mode__icon"
          ><component :is="item.icon" :size="22" aria-hidden="true"
        /></span>
        <span v-if="mode === item.id" class="scout-chip" data-tone="accent">
          {{ t("scout.design.selected") }}
        </span>
      </div>
      <div class="scout-mode__copy">
        <h2 class="scout-mode__title">
          {{
            t(
              item.id === "ai"
                ? "scout.design.modes.ai.title"
                : `scout.workspace.modes.${item.id}.title`,
            )
          }}
        </h2>
        <p class="scout-mode__description">
          {{ t(`scout.design.modes.${item.id}.description`) }}
        </p>
      </div>
      <p class="scout-mode__detail">
        {{ t(`scout.design.modes.${item.id}.detail`) }}
      </p>
      <button
        type="button"
        class="btn"
        :class="mode === item.id ? 'btn-primary' : 'btn-outline-secondary'"
        :disabled="disabled"
        @click="open(item.id)"
      >
        {{ t(`scout.design.modes.${item.id}.action`) }}
      </button>
    </article>
  </div>
</template>
