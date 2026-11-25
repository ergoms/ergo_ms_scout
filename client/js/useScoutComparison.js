import { computed, onUnmounted, reactive, ref, watch } from 'vue'
import { apiClient } from '@/js/api/manager'
import { scoutEndpoints as endpoints } from './endpoints.js'
import {
  comparisonParams,
  compatibleRunMetadata,
  resultLink,
  runFromResultLink,
  validRunId,
} from './comparisonState.js'
export function useScoutComparison(routeSelection, persist) {
  const draft = reactive({ base: '', target: '', field: '' }),
    pair = ref(null),
    job = ref(null),
    fields = ref([]),
    loading = ref(false),
    pairError = ref(null),
    result = ref(null),
    changes = ref([]),
    cursor = ref(null),
    comparing = ref(false),
    error = ref(null)
  let pairRequest = 0,
    compareRequest = 0,
    disposed = false
  const origin = window.location.origin
  const ids = computed(() => ({
    base: runFromResultLink(draft.base, origin),
    target: runFromResultLink(draft.target, origin),
  }))
  const linksStamp = computed(() => JSON.stringify(ids.value))
  const inputStamp = computed(() =>
    JSON.stringify([ids.value.base, ids.value.target, draft.field.trim()]),
  )
  const validLinks = computed(
    () => !!ids.value.base && !!ids.value.target && ids.value.base !== ids.value.target,
  )
  const canCompare = computed(
    () =>
      validLinks.value &&
      !!pair.value &&
      compatibleRunMetadata(pair.value.base, pair.value.target) &&
      !!draft.field.trim() &&
      !loading.value &&
      !comparing.value,
  )
  const quiet = { quietStatuses: [400, 401, 403, 404, 409, 410, 429, 500, 502, 503, 504] }
  async function get(url, params = {}) {
    try {
      return await apiClient.get(url, params, true, quiet)
    } catch (failure) {
      return {
        success: false,
        status: failure.response?.status,
        data: failure.response?.data,
        message: failure.message,
      }
    }
  }
  function problem(response) {
    const data = response.data || {}
    return {
      key:
        response.status === 404
          ? 'notFound'
          : response.status === 403 || response.status === 401
            ? 'forbidden'
            : response.status === 410
              ? 'expired'
              : data.code || 'requestFailed',
      detail: data.detail || '',
      run: data.run_public_id || data.details?.run_public_id || '',
    }
  }
  function clearResult() {
    compareRequest++
    result.value = null
    changes.value = []
    cursor.value = null
    comparing.value = false
    error.value = null
  }
  watch(
    () => [draft.base, draft.target],
    () => {
      pairRequest++
      pair.value = null
      job.value = null
      fields.value = []
      pairError.value = null
      loading.value = false
      clearResult()
    },
    { flush: 'sync' },
  )
  watch(() => draft.field, clearResult, { flush: 'sync' })
  async function loadPair() {
    if (disposed || !validLinks.value || loading.value) return
    const request = ++pairRequest,
      stamp = linksStamp.value,
      { base, target } = ids.value
    loading.value = true
    pairError.value = null
    pair.value = null
    fields.value = []
    job.value = null
    clearResult()
    const responses = await Promise.all([
      get(endpoints.run(base), { summary: 1 }),
      get(endpoints.run(target), { summary: 1 }),
    ])
    if (disposed || request !== pairRequest || stamp !== linksStamp.value) return
    const failed = responses.find((response) => !response.success)
    if (failed) {
      loading.value = false
      pairError.value = problem(failed)
      return
    }
    pair.value = { base: responses[0].data, target: responses[1].data }
    if (!compatibleRunMetadata(pair.value.base, pair.value.target)) {
      loading.value = false
      pairError.value = { key: 'metadataMismatch', detail: '' }
      return
    }
    const response = await get(endpoints.job(pair.value.target.job_public_id))
    if (disposed || request !== pairRequest || stamp !== linksStamp.value) return
    if (response.success) {
      job.value = response.data
      if (response.data.revision?.plan_hash === pair.value.target.plan_hash) {
        fields.value = response.data.revision.fields
          .filter((field) => !field.many)
          .map((field) => field.name)
        if (!draft.field) draft.field = fields.value[0] || ''
      }
    }
    // A job's latest revision is not necessarily the revision of these historical runs.
    loading.value = false
  }
  async function compare(append = false) {
    if (disposed || !canCompare.value || (append && !cursor.value)) return
    if (draft.field !== draft.field.trim()) draft.field = draft.field.trim()
    const request = ++compareRequest,
      stamp = inputStamp.value,
      base = ids.value.base,
      target = ids.value.target,
      field = draft.field.trim(),
      next = append ? cursor.value : null
    comparing.value = true
    error.value = null
    if (!append) {
      result.value = null
      changes.value = []
      cursor.value = null
    }
    const response = await get(endpoints.compareRuns(target), comparisonParams(base, field, next))
    if (disposed || request !== compareRequest || stamp !== inputStamp.value) return
    comparing.value = false
    if (response.success) {
      result.value = response.data
      changes.value = append ? [...changes.value, ...response.data.changes] : response.data.changes
      cursor.value = response.data.next_cursor || null
    } else {
      error.value = problem(response)
      if (response.status === 410) {
        result.value = null
        changes.value = []
        cursor.value = null
      }
    }
  }
  function swap() {
    if (loading.value || comparing.value) return
    const previous = draft.base
    draft.base = draft.target
    draft.target = previous
  }
  async function selectHistory(selection) {
    if (
      disposed ||
      loading.value ||
      comparing.value ||
      !validRunId(selection.base) ||
      !validRunId(selection.target) ||
      selection.base === selection.target
    )
      return
    draft.base = resultLink(selection.base, origin)
    draft.target = resultLink(selection.target, origin)
    const names = (Array.isArray(selection.fields) ? selection.fields : []).filter(
      (name) => typeof name === 'string',
    )
    if (!names.includes(draft.field)) draft.field = names[0] || ''
    await loadPair()
    if (
      !disposed &&
      pair.value &&
      ids.value.base === selection.base &&
      ids.value.target === selection.target &&
      !fields.value.length
    )
      fields.value = names
  }
  watch(
    routeSelection,
    (value) => {
      const base = validRunId(value.base) ? value.base.toLowerCase() : null,
        target = validRunId(value.target) ? value.target.toLowerCase() : null,
        field = typeof value.field === 'string' ? value.field : ''
      const pairChanged = ids.value.base !== base || ids.value.target !== target
      if (ids.value.base !== base) draft.base = resultLink(base, origin)
      if (ids.value.target !== target) draft.target = resultLink(target, origin)
      if (draft.field.trim() !== field.trim()) draft.field = field
      if (pairChanged && validLinks.value) loadPair()
    },
    { immediate: true },
  )
  watch(inputStamp, () => {
    const { base, target } = ids.value
    const field = draft.field.trim()
    const current = routeSelection.value
    if (
      base === (current.base ?? null) &&
      target === (current.target ?? null) &&
      field === (current.field ?? '')
    )
      return
    persist({ base: base || undefined, target: target || undefined, field: field || undefined })
  })
  onUnmounted(() => {
    disposed = true
    pairRequest++
    compareRequest++
  })
  return {
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
  }
}
