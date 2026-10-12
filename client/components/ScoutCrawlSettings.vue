<script setup>
import FormField from '@/components/FormField.vue'
import { useAppI18n } from '@/i18n/useAppI18n.js'

const mode = defineModel('mode')
const hosts = defineModel('hosts')
const visit = defineModel('visit')
const extract = defineModel('extract')
const depth = defineModel('depth')
const pages = defineModel('pages')
const previewUrl = defineModel('previewUrl')
const limits = defineModel('limits')
const { t } = useAppI18n()
const options = [
  ['max_seconds', 'crawlSeconds', 1, 3600],
  ['max_total_bytes', 'crawlBytes', 1, 50000000],
  ['frontier_cap', 'frontierCap', 1, 500],
  ['fetch_delay_ms', 'fetchDelay', 0, 5000],
  ['parallel_fetch', 'parallelFetch', 1, 4],
  ['preview_pages', 'previewPages', 1, 5],
]
function updateLimit(key, value) { limits.value = { ...limits.value, [key]: Number(value) } }
</script>

<template>
  <div class="d-flex flex-wrap gap-3 py-2">
    <label><input v-model="mode" type="radio" value="pages"> {{ t('scout.flow.pagesOnly') }}</label>
    <label><input v-model="mode" type="radio" value="site"> {{ t('scout.flow.crawlSite') }}</label>
  </div>
  <template v-if="mode === 'site'">
    <FormField :label="t('scout.flow.allowedHosts')" label-for="scout-hosts"><input id="scout-hosts" v-model="hosts" class="form-control" type="text"></FormField>
    <FormField :label="t('scout.flow.visitPrefix')" label-for="scout-visit"><input id="scout-visit" v-model="visit" class="form-control" type="text" placeholder="/catalog"></FormField>
    <FormField :label="t('scout.flow.extractPrefix')" label-for="scout-extract"><input id="scout-extract" v-model="extract" class="form-control" type="text" placeholder="/item"></FormField>
    <FormField :label="t('scout.flow.previewUrl')" label-for="scout-preview-url"><input id="scout-preview-url" v-model="previewUrl" class="form-control" type="url"></FormField>
    <FormField :label="t('scout.flow.depth')" label-for="scout-depth"><input id="scout-depth" v-model="depth" class="form-control" type="number" min="0" max="5"></FormField>
    <FormField :label="t('scout.flow.crawlPages')" label-for="scout-pages"><input id="scout-pages" v-model="pages" class="form-control" type="number" min="1" max="200"></FormField>
    <FormField v-for="[key, label, min, max] in options" :key="key" :label="t(`scout.flow.${label}`)" :label-for="`scout-${key}`">
      <input :id="`scout-${key}`" :value="limits[key]" class="form-control" type="number" :min="min" :max="max" @input="updateLimit(key, $event.target.value)">
    </FormField>
  </template>
</template>
