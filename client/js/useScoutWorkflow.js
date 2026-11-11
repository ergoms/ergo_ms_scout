import { computed, onUnmounted, reactive, ref, watch } from 'vue'
import { useRouteQueryState } from '@/composables/useRouteQueryState.js'
import { useAppI18n } from '@/i18n/useAppI18n.js'
import { apiClient } from '@/js/api/manager'
import { confirmAction } from '@/js/utils/confirm.js'
import { handleApiError, showError } from '@/js/utils/toast.js'
import { downloadMediaFromResult } from '@/js/utils/mediaDownload.js'
import { scoutEndpoints } from './endpoints.js'
import { defaultCrawlLimits } from './crawlLimits.js'
import {
  ACTIVE_STATUSES,
  EXPORTABLE_STATUSES,
  TERMINAL_STATUSES,
  fieldProblems,
  planMatchesRun,
  validSource,
  workflowStage,
} from './workflowState.js'

function blankField(scope = 'page') {
  return { name: '', kind: 'css', selector: '', scope, many: false }
}

function blankForm() {
  return {
    title: '',
    url: '',
    recordMode: 'page',
    recordKind: 'css',
    recordSelector: '',
    crawlMode: 'pages',
    allowedHosts: '',
    visitPrefix: '',
    extractPrefix: '',
    crawlDepth: 2,
    crawlPages: 50,
    crawlLimits: defaultCrawlLimits(),
    previewUrl: '',
    fields: [blankField()],
  }
}

function updateFirst(items = [], first) {
  return [...(first ? [first] : []), ...items.slice(1)]
}

function updateRule(items, value) {
  const pattern = value.trim()
  return updateFirst(items, pattern ? { kind: items?.[0]?.kind || 'prefix', pattern } : null)
}

export function useScoutWorkflow() {
  const { t } = useAppI18n()
  const { state: query, patchState } = useRouteQueryState({
    job: { default: '' },
    run: { default: '' },
    mode: { default: 'manual', enum: ['manual', 'visual', 'ai'] },
  })
  const form = ref(blankForm())
  const run = ref(null)
  const records = ref([])
  const editorStep = ref('mode')
  const confirmationOpen = ref(null)
  const returning = ref('')
  const mode = computed({
    get: () => query.value.mode,
    set: (value) => patchState({ mode: value }),
  })
  const busy = ref(false)
  const jobLoading = ref(false)
  const runLoading = ref(false)
  const exporting = ref(false)
  const connectionLost = ref(false)
  const recordsError = ref(false)
  const pageError = ref('')
  const savedStamp = ref('')
  const savedRevision = ref(null)
  const loadedJob = ref('')
  const exportKind = ref('json')
  const errors = reactive({ url: '', card: '', fields: [] })
  const visualPlanValid = ref(false)
  let context = 0
  let operation = 0
  let jobRequest = 0
  let runRequest = 0
  let recordsLoad = null
  let pollTimer = 0
  let disposed = false
  let draftVersion = 0
  let applyingJob = false

  function payload() {
    const f = form.value
    const cards = f.recordMode === 'cards'
    const revision = savedRevision.value
    const crawl = { ...revision?.crawl }
    if (f.crawlMode === 'pages') {
      delete crawl.page_types
      delete crawl.sitemap_url
      delete crawl.sitemap_from_robots
    }
    return {
      title: f.title.trim(),
      seed_urls: updateFirst(revision?.seed_urls, f.url.trim()),
      record:
        cards && f.recordSelector.trim()
          ? { kind: f.recordKind, selector: f.recordSelector.trim() }
          : null,
      fields: f.fields.map((field) => ({
        name: field.name.trim(),
        kind: field.kind,
        selector: field.selector.trim(),
        scope: cards && field.scope === 'record' ? 'record' : 'page',
        many: field.many,
        ...(field.conversion ? { conversion: field.conversion } : {}),
      })),
      crawl: {
        ...crawl,
        ...f.crawlLimits,
        mode: f.crawlMode,
        preview_urls: updateFirst(crawl?.preview_urls, f.previewUrl.trim()),
        allowed_hosts: f.allowedHosts
          .split(',')
          .map((item) => item.trim())
          .filter(Boolean),
        visit: updateRule(crawl?.visit, f.visitPrefix),
        extract_on: updateRule(crawl?.extract_on, f.extractPrefix),
        max_depth: f.crawlDepth === '' ? 2 : Number(f.crawlDepth),
        max_pages: f.crawlPages === '' ? 50 : Number(f.crawlPages),
      },
      ...(revision?.identity ? { identity: revision.identity } : {}),
      ...(revision?.proxy_profile_revision_public_id
        ? { proxy_profile_revision_public_id: revision.proxy_profile_revision_public_id }
        : {}),
    }
  }
  const stamp = computed(() => JSON.stringify(payload()))
  const dirty = computed(() => Boolean(savedStamp.value) && stamp.value !== savedStamp.value)
  const loading = computed(() => busy.value || jobLoading.value || runLoading.value)
  const stage = computed(() =>
    workflowStage(run.value, editorStep.value, confirmationOpen.value, returning.value),
  )
  const previewRows = computed(() => (Array.isArray(run.value?.preview) ? run.value.preview : []))
  const previewError = computed(
    () => run.value?.preview?.error?.detail || run.value?.preview?.error?.message || '',
  )
  const planReady = computed(
    () =>
      loadedJob.value === query.value.job &&
      run.value?.public_id === query.value.run &&
      planMatchesRun(
        run.value,
        loadedJob.value,
        savedRevision.value?.plan_hash,
        stamp.value,
        savedStamp.value,
      ),
  )
  const confirmationPlan = computed(() => (planReady.value ? savedRevision.value : null))
  const canConfirm = computed(
    () =>
      !loading.value &&
      !connectionLost.value &&
      !returning.value &&
      planReady.value &&
      previewRows.value.length > 0,
  )
  const canExport = computed(
    () => records.value.length > 0 && EXPORTABLE_STATUSES.includes(run.value?.status),
  )
  const progress = computed(() => {
    const records = run.value?.record_count || 0
    if (run.value?.progress) return { ...run.value.progress, records }
    const tasks = Array.isArray(run.value?.tasks) ? run.value.tasks : []
    return {
      fetched: tasks.filter((task) => task.kind === 'fetch' && task.status === 'succeeded').length,
      extracted: tasks.filter((task) => task.kind === 'extract' && task.status === 'succeeded')
        .length,
      records,
      errors: tasks.filter((task) => task.status === 'failed').length,
    }
  })

  watch(
    stamp,
    () => {
      if (applyingJob) return
      draftVersion += 1
      confirmationOpen.value = false
    },
    { flush: 'sync' },
  )

  function applyJob(job) {
    const revision = job?.revision || {}
    const crawl = revision.crawl || {}
    applyingJob = true
    form.value = {
      ...blankForm(),
      title: job?.title || '',
      url: revision.seed_urls?.[0] || '',
      recordMode: revision.record?.selector ? 'cards' : 'page',
      recordKind: revision.record?.kind === 'xpath' ? 'xpath' : 'css',
      recordSelector: revision.record?.selector || '',
      crawlMode: crawl.mode === 'site' ? 'site' : 'pages',
      allowedHosts: Array.isArray(crawl.allowed_hosts) ? crawl.allowed_hosts.join(', ') : '',
      visitPrefix: crawl.visit?.[0]?.pattern || '',
      extractPrefix: crawl.extract_on?.[0]?.pattern || '',
      crawlDepth: crawl.max_depth ?? 2,
      crawlPages: crawl.max_pages ?? 50,
      crawlLimits: defaultCrawlLimits(crawl),
      previewUrl: crawl.preview_urls?.[0] || '',
      fields:
        Array.isArray(revision.fields) && revision.fields.length
          ? revision.fields.map((field) => ({ ...blankField(), ...field }))
          : [blankField()],
    }
    errors.fields = form.value.fields.map(() => '')
    loadedJob.value = job.public_id
    savedRevision.value = revision
    savedStamp.value = stamp.value
    applyingJob = false
  }

  async function loadJob(publicId) {
    if (loadedJob.value === publicId) return
    const request = ++jobRequest
    const version = draftVersion
    const owner = context
    jobLoading.value = true
    try {
      const response = await apiClient.get(scoutEndpoints.job(publicId))
      if (disposed || request !== jobRequest || owner !== context || query.value.job !== publicId)
        return
      if (!response.success) {
        pageError.value = response.message || t('scout.workspace.loadJobFailed')
        return
      }
      if (version !== draftVersion) {
        pageError.value = t('scout.workspace.draftKept')
        return
      }
      applyJob(response.data)
      editorStep.value = 'source'
    } catch (error) {
      if (owner !== context || request !== jobRequest || disposed) return
      pageError.value = t('scout.workspace.loadJobFailed')
      logError('SCOUT: не удалось прочитать задание', error)
    } finally {
      if (request === jobRequest) jobLoading.value = false
    }
  }

  function schedulePoll() {
    window.clearTimeout(pollTimer)
    if (disposed || !query.value.run) return
    pollTimer = window.setTimeout(() => loadRun(query.value.run, true), 2000)
  }

  function loadRecords(data) {
    const publicId = data.public_id
    const full = TERMINAL_STATUSES.includes(data.status)
    const previous = recordsLoad?.publicId === publicId ? recordsLoad : null
    if (previous?.full === full) return previous.promise

    const load = { publicId, full }
    const owner = context
    const current = () =>
      !disposed && owner === context && recordsLoad === load && query.value.run === publicId
    load.promise = (async () => {
      // When a run finishes during a recent-records request, read the final pages after it.
      await previous?.promise
      if (!current()) return
      try {
        const rows = []
        let cursor
        do {
          const params = full
            ? { limit: 200, ...(cursor ? { cursor } : {}) }
            : { limit: 20, order: 'recent' }
          const response = await apiClient.get(scoutEndpoints.records(publicId), params)
          if (!current()) return
          recordsError.value = !response.success
          if (!response.success) return
          if (Array.isArray(response.data.records)) rows.push(...response.data.records)
          cursor = full ? response.data.next_cursor : null
        } while (cursor)
        records.value = full ? rows : rows.reverse()
      } catch (error) {
        if (!current()) return
        recordsError.value = true
        logError('SCOUT: не удалось прочитать записи', error)
      } finally {
        if (recordsLoad === load) recordsLoad = null
      }
    })()
    recordsLoad = load
    return load.promise
  }

  function clearRecords() {
    recordsLoad = null
    records.value = []
    recordsError.value = false
  }

  function refreshRecords(data) {
    if (data.record_count) return loadRecords(data)
    clearRecords()
  }

  function invalidateRunLoad() {
    runRequest += 1
    runLoading.value = false
  }

  function clearRun(updateQuery = true) {
    window.clearTimeout(pollTimer)
    invalidateRunLoad()
    clearRecords()
    run.value = null
    returning.value = ''
    confirmationOpen.value = null
    connectionLost.value = false
    if (updateQuery) patchState({ run: '' })
  }

  function resetForm() {
    context += 1
    operation += 1
    jobRequest += 1
    busy.value = false
    jobLoading.value = false
    clearRun(false)
    form.value = blankForm()
    loadedJob.value = ''
    savedStamp.value = ''
    savedRevision.value = null
    pageError.value = ''
    Object.assign(errors, { url: '', card: '', fields: [] })
    editorStep.value = 'mode'
    patchState({ job: '', run: '', mode: 'manual' })
  }

  function receiveRun(data) {
    if (returning.value && TERMINAL_STATUSES.includes(data.status)) {
      if (returning.value === 'new') resetForm()
      else {
        clearRun()
        editorStep.value = 'source'
      }
      return false
    }
    run.value = data
    if (ACTIVE_STATUSES.includes(data.status)) schedulePoll()
    return true
  }

  async function loadRun(publicId, quiet = false) {
    if (!publicId || disposed) return
    window.clearTimeout(pollTimer)
    const request = ++runRequest
    const owner = context
    const current = () =>
      !disposed && request === runRequest && owner === context && query.value.run === publicId
    if (!quiet) runLoading.value = true
    try {
      let summary = quiet
      let response = await apiClient.get(scoutEndpoints.run(publicId), summary ? { summary: 1 } : {})
      if (!current()) return
      if (summary && response.success && !ACTIVE_STATUSES.includes(response.data.status)) {
        // The full response supplies the preview and the final journal once polling stops.
        summary = false
        response = await apiClient.get(scoutEndpoints.run(publicId))
        if (!current()) return
      }
      if (!response.success) {
        connectionLost.value = true
        schedulePoll()
        return
      }
      connectionLost.value = false
      const data = summary ? { ...run.value, ...response.data } : response.data
      if (!receiveRun(data)) return
      if (response.data.job_public_id && !query.value.job)
        patchState({ job: response.data.job_public_id })
      const recordsRead = refreshRecords(data)
      if (!quiet) await recordsRead
    } catch (error) {
      if (!current()) return
      connectionLost.value = true
      logError('SCOUT: не удалось прочитать запуск', error)
      schedulePoll()
    } finally {
      if (request === runRequest) runLoading.value = false
    }
  }

  function validate() {
    errors.url = validSource(form.value.url) ? '' : t('scout.flow.urlRequired')
    errors.card =
      form.value.recordMode === 'cards' && !form.value.recordSelector.trim()
        ? t('scout.flow.cardRequired')
        : ''
    errors.fields = fieldProblems(form.value.fields).map((problem) =>
      problem ? t(`scout.workspace.fieldErrors.${problem}`) : '',
    )
    return (
      !errors.url &&
      !errors.card &&
      form.value.fields.length > 0 &&
      errors.fields.every((item) => !item)
    )
  }

  async function checkPage() {
    if (
      !['manual', 'visual'].includes(mode.value) ||
      (mode.value === 'visual' && !visualPlanValid.value) ||
      loading.value ||
      !validate()
    )
      return
    const owner = context
    const currentOperation = ++operation
    const version = draftVersion
    const jobId = query.value.job
    if (jobId && loadedJob.value !== jobId) return
    const previousRun = query.value.run
    busy.value = true
    pageError.value = ''
    try {
      let savedJobId = jobId
      if (!jobId || dirty.value) {
        const saved = jobId
          ? await apiClient.patch(scoutEndpoints.job(jobId), payload())
          : await apiClient.post(scoutEndpoints.jobs, payload())
        if (
          disposed ||
          owner !== context ||
          currentOperation !== operation ||
          query.value.job !== jobId ||
          query.value.run !== previousRun
        )
          return
        if (!saved.success) {
          pageError.value = saved.message || t('scout.flow.checkFailed')
          return
        }
        if (version !== draftVersion) {
          pageError.value = t('scout.workspace.draftKept')
          return
        }
        applyJob(saved.data)
        savedJobId = saved.data.public_id
      }
      await patchState({ job: savedJobId, run: '' })
      if (
        disposed ||
        owner !== context ||
        currentOperation !== operation ||
        query.value.job !== savedJobId ||
        query.value.run
      )
        return
      const explored = await apiClient.post(scoutEndpoints.explore(savedJobId), {})
      if (
        disposed ||
        owner !== context ||
        currentOperation !== operation ||
        query.value.job !== savedJobId ||
        query.value.run
      )
        return
      if (!explored.success) {
        pageError.value = explored.message || t('scout.flow.checkFailed')
        return
      }
      clearRecords()
      confirmationOpen.value = false
      run.value = explored.data
      await patchState({ run: explored.data.public_id })
    } catch (error) {
      if (disposed || owner !== context || currentOperation !== operation) return
      pageError.value = t('scout.flow.checkFailed')
      logError('SCOUT: не удалось подготовить предпросмотр', error)
    } finally {
      if (currentOperation === operation) busy.value = false
    }
  }

  async function act(endpoint, label) {
    if (!query.value.run || busy.value) return
    const id = query.value.run
    const owner = context
    const currentOperation = ++operation
    invalidateRunLoad()
    window.clearTimeout(pollTimer)
    busy.value = true
    try {
      const response = await apiClient.post(endpoint(id), {})
      if (disposed || owner !== context || currentOperation !== operation || query.value.run !== id)
        return
      if (!response.success) {
        returning.value = ''
        showError(response.message || label)
        await loadRun(id, true)
        return
      }
      connectionLost.value = false
      if (receiveRun(response.data)) refreshRecords(response.data)
    } catch (error) {
      if (disposed || owner !== context || currentOperation !== operation || query.value.run !== id)
        return
      returning.value = ''
      await handleApiError(error, label)
      await loadRun(id, true)
    } finally {
      if (currentOperation === operation) busy.value = false
    }
  }

  async function confirmAndStart() {
    if (!canConfirm.value) return
    const id = query.value.run
    const owner = context
    const currentOperation = ++operation
    busy.value = true
    invalidateRunLoad()
    window.clearTimeout(pollTimer)
    try {
      if (!run.value.approved) {
        const approved = await apiClient.post(scoutEndpoints.approve(id), {})
        if (
          disposed ||
          owner !== context ||
          currentOperation !== operation ||
          query.value.run !== id
        )
          return
        if (!approved.success) {
          showError(approved.message || t('scout.flow.confirmStart'))
          return
        }
        receiveRun(approved.data)
      }
      if (!planReady.value) return
      const started = await apiClient.post(scoutEndpoints.start(id), {})
      if (disposed || owner !== context || currentOperation !== operation || query.value.run !== id)
        return
      if (!started.success) {
        showError(started.message || t('scout.flow.startHeld'))
        await loadRun(id, true)
        return
      }
      receiveRun(started.data)
    } catch (error) {
      if (disposed || owner !== context || currentOperation !== operation || query.value.run !== id)
        return
      await handleApiError(error, t('scout.workspace.startFailed'))
      await loadRun(id, true)
    } finally {
      if (currentOperation === operation) busy.value = false
    }
  }

  async function changeRules() {
    if (busy.value) return
    confirmationOpen.value = false
    if (!run.value || TERMINAL_STATUSES.includes(run.value.status)) {
      clearRun()
      editorStep.value = 'source'
      return
    }
    returning.value = 'editor'
    await act(scoutEndpoints.cancel, t('scout.cancel'))
  }

  async function newJob() {
    if (run.value && !TERMINAL_STATUSES.includes(run.value.status)) {
      const id = query.value.run
      const ok = await confirmAction({
        title: t('scout.newJob'),
        message: t('scout.workspace.newJobConfirm'),
      })
      if (!ok || id !== query.value.run) return
      returning.value = 'new'
      await act(scoutEndpoints.cancel, t('scout.cancel'))
      return
    }
    if (dirty.value || form.value.url || form.value.fields.some((field) => field.selector)) {
      const owner = context
      const ok = await confirmAction({
        title: t('scout.newJob'),
        message: t('scout.workspace.discardDraft'),
      })
      if (!ok || owner !== context) return
    }
    resetForm()
  }

  async function cancelRun() {
    const id = query.value.run
    const ok = await confirmAction({
      title: t('scout.cancel'),
      message: t('scout.workspace.cancelConfirm'),
    })
    if (ok && id === query.value.run) await act(scoutEndpoints.cancel, t('scout.cancel'))
  }

  async function exportFile() {
    if (!canExport.value || exporting.value) return
    const id = query.value.run
    const owner = context
    exporting.value = true
    try {
      const response = await apiClient.post(scoutEndpoints.exportRun(id), {
        kind: exportKind.value,
      })
      if (disposed || owner !== context || query.value.run !== id) return
      if (!response.success) {
        showError(response.message || t('scout.flow.download'))
        return
      }
      await downloadMediaFromResult(response.data)
    } catch (error) {
      if (!disposed && owner === context) await handleApiError(error, t('scout.flow.download'))
    } finally {
      exporting.value = false
    }
  }

  watch(
    () => query.value.job,
    (id) => {
      if (id) loadJob(id)
    },
    { immediate: true },
  )
  watch(
    () => query.value.run,
    (id, previous) => {
      window.clearTimeout(pollTimer)
      if (id !== previous) {
        invalidateRunLoad()
        clearRecords()
        connectionLost.value = false
        if (run.value?.public_id !== id) run.value = null
      }
      if (id) loadRun(id)
    },
    { immediate: true },
  )
  onUnmounted(() => {
    disposed = true
    context += 1
    window.clearTimeout(pollTimer)
  })

  return {
    visualPlanValid,
    form,
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
    jobLoading,
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
    reload: () => loadRun(query.value.run),
    pause: () => act(scoutEndpoints.pause, t('scout.pause')),
    resume: () => act(scoutEndpoints.resume, t('scout.resume')),
    addField: () => {
      form.value.fields.push(blankField(form.value.recordMode === 'cards' ? 'record' : 'page'))
      errors.fields.push('')
    },
    removeField: (index) => {
      if (form.value.fields.length > 1) {
        form.value.fields.splice(index, 1)
        errors.fields.splice(index, 1)
      }
    },
  }
}
