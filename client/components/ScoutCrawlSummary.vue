<script setup>
import { computed } from 'vue'
import { useAppI18n } from '@/i18n/useAppI18n.js'

const props = defineProps({
  run: Object,
  depth: [Number, String],
  pages: [Number, String],
  hosts: String,
  limits: Object,
  visit: String,
  extract: String,
  planDetails: Boolean,
})
const { t } = useAppI18n()
const crawl = computed(() => props.run?.crawl || {
  ...props.limits, max_depth: props.depth, max_pages: props.pages,
  allowed_hosts: [props.hosts], visit: props.visit ? [{ pattern: props.visit }] : [],
  extract_on: props.extract ? [{ pattern: props.extract }] : [],
})
function patterns(rules) {
  return (rules || [])
    .map(({ kind, pattern }) => {
      if (!props.planDetails || !kind) return pattern
      return `${t(`scout.workspace.crawlRule.${kind}`)} ${pattern}`
    })
    .join(', ')
}
</script>

<template>
  <div class="scout-crawl-summary">
    <p class="mb-1">{{ t('scout.flow.crawlBounds', { hosts: crawl.allowed_hosts?.join(', '), depth: crawl.max_depth, pages: crawl.max_pages }) }}</p>
    <p class="mb-1">{{ t('scout.flow.crawlBudget', { seconds: crawl.max_seconds, bytes: crawl.max_total_bytes, frontier: crawl.frontier_cap, delay: crawl.fetch_delay_ms, parallel: crawl.parallel_fetch }) }}</p>
    <p v-if="crawl.visit?.length || crawl.extract_on?.length" class="mb-1">{{ t('scout.flow.crawlFilters', { visit: patterns(crawl.visit) || '/', extract: patterns(crawl.extract_on) || '/' }) }}</p>
    <template v-if="planDetails">
      <p v-if="crawl.preview_pages != null" class="mb-1">
        {{ t('scout.workspace.previewPageLimit', { pages: crawl.preview_pages }) }}
      </p>
      <template v-if="crawl.preview_urls?.length">
        <p class="mb-1">{{ t('scout.workspace.previewAddresses') }}</p>
        <ul class="list-unstyled mb-1">
          <li v-for="url in crawl.preview_urls" :key="url">{{ url }}</li>
        </ul>
      </template>
      <p v-if="crawl.drop_query?.length" class="mb-1">
        {{ t('scout.workspace.ignoredQueryParams', { params: crawl.drop_query.join(', ') }) }}
      </p>
    </template>
    <p v-if="run?.frontier" class="mb-1">{{ t('scout.flow.frontierLine', run.frontier) }}</p>
    <p v-if="run?.stop_reason" class="mb-0 fw-semibold">{{ t(`scout.stopReasons.${run.stop_reason}`) }}</p>
  </div>
</template>
