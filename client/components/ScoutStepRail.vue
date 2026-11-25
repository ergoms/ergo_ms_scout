<script setup>
import { useAppI18n } from "@/i18n/useAppI18n.js";

defineProps({
  steps: { type: Array, required: true },
  interactive: { type: Boolean, default: false },
});
defineEmits(["select"]);
const { t } = useAppI18n();
</script>

<template>
  <nav class="step-rail">
    <ol class="step-rail__list">
      <li
        v-for="step in steps"
        :key="step.id"
        class="step-rail__item"
        :class="`step-rail__item--${step.state}`"
      >
        <button
          type="button"
          class="step-rail__button"
          :disabled="!interactive || step.disabled"
          :aria-current="step.state === 'current' ? 'step' : undefined"
          @click="$emit('select', step.id)"
        >
          <span class="step-rail__marker" aria-hidden="true">{{
            step.marker
          }}</span>
          <span
            v-if="step.state === 'current'"
            class="step-rail__compact scout-chip"
            data-tone="accent"
          >
            {{
              t("scout.design.stepNumber", {
                number: step.marker,
                total: steps.length,
              })
            }}
          </span>
          <span class="step-rail__label">{{ step.label }}</span>
        </button>
      </li>
    </ol>
  </nav>
</template>
