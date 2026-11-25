<script setup>
defineProps({
  items: { type: Array, required: true },
})
</script>

<template>
  <dl v-if="items.length" class="scout-run-metrics">
    <div v-for="item in items" :key="item.label" class="scout-run-metrics__item">
      <dt>{{ item.label }}</dt>
      <dd>{{ item.value }}</dd>
    </div>
  </dl>
</template>
