import { onUnmounted, ref, watch } from 'vue'
import { apiClient } from '@/js/api/manager'
import { scoutEndpoints as endpoints } from './endpoints.js'
import { validRunId } from './comparisonState.js'

export function useScoutRunHistory(contextJob) {
  const jobId = ref(''),
    jobs = ref([]),
    rows = ref([]),
    cursor = ref(null)
  const loading = ref(false),
    jobsLoading = ref(false),
    loaded = ref(false)
  const error = ref(null),
    jobsError = ref(null)
  let requestId = 0,
    disposed = false
  const quiet = { quietStatuses: [400, 401, 403, 404, 409, 410, 429, 500, 502, 503, 504] }
  async function get(url, params = {}) {
    try {
      return await apiClient.get(url, params, true, quiet)
    } catch (failure) {
      return { success: false, status: failure.response?.status }
    }
  }
  const problem = (response) => ({
    key:
      response.status === 401 || response.status === 403
        ? 'forbidden'
        : response.status === 404
          ? 'notFound'
          : 'requestFailed',
  })
  async function loadJobs() {
    if (disposed || jobsLoading.value) return
    jobsLoading.value = true
    jobsError.value = null
    const response = await get(endpoints.jobs)
    if (disposed) return
    jobsLoading.value = false
    if (response.success && Array.isArray(response.data?.jobs)) jobs.value = response.data.jobs
    else jobsError.value = problem(response)
  }
  async function load(append = false) {
    if (disposed || !validRunId(jobId.value) || loading.value || (append && !cursor.value)) return
    const id = ++requestId,
      job = jobId.value,
      next = append ? cursor.value : null
    loading.value = true
    error.value = null
    if (!append) {
      rows.value = []
      cursor.value = null
      loaded.value = false
    }
    const response = await get(endpoints.jobRuns(job), {
      limit: 50,
      ...(next ? { cursor: next } : {}),
    })
    if (disposed || id !== requestId || job !== jobId.value) return
    loading.value = false
    if (!response.success || !Array.isArray(response.data?.runs)) {
      error.value = problem(response)
      return
    }
    rows.value = [
      ...new Map(
        (append ? [...rows.value, ...response.data.runs] : response.data.runs).map((row) => [
          row.public_id,
          row,
        ]),
      ).values(),
    ]
    cursor.value = response.data.next_cursor || null
    loaded.value = true
  }
  function chooseJob(id) {
    id = validRunId(id) ? id.toLowerCase() : ''
    if (id === jobId.value) return
    ++requestId
    jobId.value = id
    rows.value = []
    cursor.value = null
    error.value = null
    loading.value = false
    loaded.value = false
    if (id) load()
  }
  watch(contextJob, chooseJob, { immediate: true })
  loadJobs()
  onUnmounted(() => {
    disposed = true
    ++requestId
  })
  return {
    jobId,
    jobs,
    rows,
    cursor,
    loading,
    jobsLoading,
    loaded,
    error,
    jobsError,
    chooseJob,
    load,
    loadJobs,
  }
}
