<script setup>
import { computed, nextTick, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { Check, ChevronDown, Globe, Image as ImageIcon, Info, MousePointer2, RefreshCw, TriangleAlert } from '@lucide/vue'
import FormCard from '@/components/FormCard.vue'
import FormField from '@/components/FormField.vue'
import ToggleSwitch from '@/components/ToggleSwitch.vue'
import { useAppI18n } from '@/i18n/useAppI18n.js'
import { useVisualSnapshot } from '../js/useVisualSnapshot.js'
import { matchSummary, visualPlanStamp } from '../js/visualState.js'
import { displayValue } from '../js/recordTable.js'
import ScoutSnapshotFrame from './ScoutSnapshotFrame.vue'
import ScoutImageDownload from './ScoutImageDownload.vue'
import { scoutEndpoints } from '../js/endpoints.js'
import ScoutPrototypeTree from '../prototypes/ScoutPrototypeTree.vue'
import { useInspectorTree, nodeLabel } from '../prototypes/useInspectorTree.js'

const form = defineModel({ type: Object, required: true })
const props = defineProps({ disabled: Boolean, inspectorPrototype: Boolean })
const emit = defineEmits(['valid', 'preview', 'manual', 'presentation', 'back'])
const { t } = useAppI18n()
const design = (key, ...args) => t(`scout.visualDesign.${key}`, ...args)
const active = ref(true)
const editor = ref(null)
const compact = ref(false)
const urlInput = ref(null)
const widths = reactive({ tree: 240, field: 340 })
let resizeObserver
let dragCleanup = () => {}
function setWidth(area, value) {
  widths[area] = Math.max(area === 'tree' ? 210 : 300, Math.min(area === 'tree' ? 300 : 400, value))
}
function resizeKey(area, event) {
  if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return
  event.preventDefault()
  setWidth(
    area,
    event.key === 'Home'
      ? 0
      : event.key === 'End'
        ? 1000
        : widths[area] + (event.key === 'ArrowRight' ? 10 : -10) * (area === 'field' ? -1 : 1),
  )
}
function resizeStart(area, event) {
  if (event.button !== 0) return
  event.preventDefault()
  event.currentTarget.setPointerCapture(event.pointerId)
  dragCleanup()
  const start = event.clientX,
    width = widths[area]
  const move = (e) => setWidth(area, width + (e.clientX - start) * (area === 'field' ? -1 : 1))
  dragCleanup = () => {
    window.removeEventListener('pointermove', move)
    window.removeEventListener('pointerup', dragCleanup)
  }
  window.addEventListener('pointermove', move)
  window.addEventListener('pointerup', dragCleanup, { once: true })
}
async function selectTab(tab) {
  prototypeTab.value = tab
  showTree.value = true
  showInspector.value = true
  await nextTick()
  const panel = editor.value?.querySelector(
    tab === 'tree'
      ? '.scout-prototype-tree'
      : tab === 'field'
        ? '.scout-visual-rules'
        : '.scout-visual-document',
  )
  panel?.focus({ preventScroll: true })
  panel?.scrollIntoView({ block: 'nearest' })
}
function tabKey(event, index) {
  if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return
  event.preventDefault()
  const tabs = ['page', 'tree', 'field']
  const next =
    event.key === 'Home'
      ? 0
      : event.key === 'End'
        ? 2
        : (index + (event.key === 'ArrowRight' ? 1 : 2)) % 3
  prototypeTab.value = tabs[next]
  showTree.value = true
  showInspector.value = true
  editor.value?.querySelectorAll('.scout-prototype-tabs button')[next]?.focus()
}
const manualRule = ref(false)
const primaryLabel = computed(() =>
  !snapshotReady.value
    ? snapshotExpired.value
      ? design('reload')
      : t('scout.visual.load')
      : checking.value
        ? t('scout.visual.checking')
        : checked.value
          ? t(
              target.value === 'record'
                ? 'scout.visual.acceptCard'
                : editing.value >= 0
                  ? 'scout.product.saveChanges'
                  : 'scout.visual.acceptField',
            )
          : valid.value && !canCancel.value
            ? `${design('preview')} · ${design('fieldCount', fields.value.length)}`
            : t('scout.product.check'),
)
const primaryDisabled = computed(() => {
  if (props.disabled || status.value === 'loading' || checking.value) return true
  if (!snapshotReady.value) return false
  if (valid.value && !canCancel.value) return false
  if (tree.loading.value) return true
  return checked.value
    ? !acceptable.value || (target.value === 'field' && duplicateName.value)
    : (target.value === 'field' && (!cardReady.value || !draft.name.trim())) ||
        (manualRule.value
          ? !(target.value === 'record' ? recordSelector.value : draft.selector).trim()
          : !selectedNode.value)
})
function primaryAction() {
  if (!snapshotReady.value) {
    load()
    return
  }
  if (checked.value) accept()
  else if (valid.value && !canCancel.value) emit('preview')
  else verify(manualRule.value ? undefined : selectedNode.value)
}
async function cancelLoading() {
  // Invalidate polling and late responses. The server expires abandoned snapshots normally.
  active.value = false
  active.value = true
  await nextTick()
  urlInput.value?.focus()
}
onMounted(() => {
  resizeObserver = new ResizeObserver(([entry]) => {
    compact.value = entry.contentRect.width < 1000
  })
  if (editor.value) resizeObserver.observe(editor.value)
})
onUnmounted(() => {
  resizeObserver?.disconnect()
  dragCleanup()
})
const url = computed(() => form.value.url)
const { snapshot, status, error, checking, load, check, invalidateCheck } = useVisualSnapshot(
  url,
  active,
  computed(() => ({ proxy_profile_revision_public_id: form.value.proxyRevision || null })),
)
const target = ref(form.value.recordMode === 'cards' ? 'record' : 'field')
const ancestors = ref([])
const selectedNode = ref('')
const hoveredNode = ref('')
const cardNodeIds = ref([])
const prototypeTab = ref('page')
const showTree = ref(true)
const showInspector = ref(true)
const tree = useInspectorTree(
  snapshot,
  computed(() => props.inspectorPrototype),
)
const snapshotExpired = computed(() => status.value === 'expired' || tree.invalid.value)
const snapshotReady = computed(() => status.value === 'ready' && !tree.invalid.value)
const recordSelector = ref(form.value.recordSelector || '')
const draft = reactive({ name: 'field_1', selector: '', many: false, scope: 'page' })
const editing = ref(-1)
const canCancel = computed(() => editing.value >= 0 || manualRule.value || !!selectedNode.value)
const lastAccepted = ref(null)
const result = ref(null)
const checkedStamp = ref('')
const acceptedStamp = ref('')
const cardStamp = ref('')
const verifiedFields = reactive(new Map())
let applying = false
const recordRule = computed(() =>
  form.value.recordMode === 'cards'
    ? { kind: form.value.recordKind, selector: form.value.recordSelector }
    : null,
)
const currentCardStamp = computed(() => JSON.stringify(recordRule.value))
const cardReady = computed(
  () =>
    form.value.recordMode === 'page' ||
    (cardStamp.value === currentCardStamp.value && !!form.value.recordSelector),
)
const candidateStamp = computed(() =>
  JSON.stringify({
    target: target.value,
    recordSelector: recordSelector.value,
    record: recordRule.value,
    ...draft,
  }),
)
const summary = computed(() => matchSummary(result.value))
const checked = computed(
  () =>
    status.value === 'ready' &&
    (!props.inspectorPrototype || !tree.invalid.value) &&
    !!result.value &&
    checkedStamp.value === candidateStamp.value &&
    !checking.value,
)
const acceptable = computed(
  () =>
    checked.value &&
    (target.value === 'record' ? result.value.row_count > 0 : summary.value.matches > 0),
)
const fields = computed(() =>
  form.value.fields
    .map((field, index) => ({ ...field, index }))
    .filter((field) => field.name || field.selector),
)
const valid = computed(
  () =>
    status.value === 'ready' &&
    fields.value.length > 0 &&
    form.value.fields.every((field) => field.name.trim() && field.selector.trim()) &&
    cardReady.value &&
    (!props.inspectorPrototype || !tree.invalid.value) &&
    acceptedStamp.value === visualPlanStamp(form.value),
)
function fieldVerified(field) {
  return (
    status.value === 'ready' &&
    !tree.invalid.value &&
    cardReady.value &&
    verifiedFields.get(JSON.stringify(form.value.fields[field.index])) === snapshot.value?.public_id
  )
}
const pendingFields = computed(() => fields.value.filter((field) => !fieldVerified(field)))
const acceptedField = computed(() => {
  if (canCancel.value || target.value !== 'field' || !lastAccepted.value) return null
  return fields.value.find((field) => field.name === lastAccepted.value.name && fieldVerified(field)) || null
})
const displayedNodeId = computed(() => selectedNode.value || (acceptedField.value ? lastAccepted.value.nodeId : ''))
const displayedNode = computed(() => {
  const detail = acceptedField.value ? lastAccepted.value.detail : tree.detail.value
  return detail?.node.node_id === displayedNodeId.value ? detail : null
})
const displayedLabel = computed(() => {
  const node = displayedNode.value?.node
  return node ? nodeLabel(node) : acceptedField.value?.selector || draft.selector
})
const displayedPath = computed(() => {
  const parent = displayedNode.value?.ancestors.at(-1)
  return parent ? `${nodeLabel(parent)} › ${nodeLabel(displayedNode.value.node)}` : displayedLabel.value
})
const displayedImage = computed(() => {
  const detail = displayedNode.value
  return detail?.node.tag === 'img' || (acceptedField.value || (checked.value ? result.value.field : null))?.value_source === 'image'
})
const canDownloadImage = computed(() => {
  const detail = displayedNode.value
  return snapshotReady.value && !tree.loading.value && detail?.node.tag === 'img' &&
    detail.snapshot_public_id === snapshot.value?.public_id && detail.tree_version === snapshot.value.tree_version
})
const showDraft = computed(() => !props.inspectorPrototype || canCancel.value || target.value === 'record')
const presentation = computed(() => {
  const title = design('title')
  const state = (key, tone, heading = title) => ({ title: heading, status: design(key), tone })
  if (snapshotExpired.value) return state('needsSnapshot', 'warning', design('expiredTitle'))
  if (status.value === 'loading') return state('loading', 'info')
  if (!snapshotReady.value && error.value) return state('loadError', 'danger')
  if (snapshotReady.value && error.value) return state('ruleError', 'danger', design('ruleErrorTitle'))
  if (checked.value && acceptable.value) return state('checked', 'success')
  if (checked.value) return state('noMatches', 'warning')
  if (canCancel.value && snapshotReady.value) return state('unchecked', 'warning')
  if (snapshotReady.value && fields.value.length)
    return { title, status: design('savedCount', fields.value.length), tone: pendingFields.value.length ? 'warning' : 'success' }
  return snapshotReady.value ? state('ready', 'success') : state('waiting', 'neutral')
})
const previewHint = computed(() => {
  if (props.disabled) return t('scout.product.previewWait')
  if (status.value === 'loading') return t('scout.visual.loading')
  if (tree.invalid.value || status.value === 'expired') return t('scout.product.previewReload')
  if (status.value !== 'ready') return t('scout.product.previewLoad')
  if (!cardReady.value) return t('scout.product.previewCard')
  if (!fields.value.length) return t('scout.product.previewAddField')
  if (pendingFields.value.length)
    return t('scout.product.previewFields', {
      names: pendingFields.value
        .slice(0, 3)
        .map((field) => field.name || `#${field.index + 1}`)
        .join(', '),
      count: pendingFields.value.length,
    })
  if (!valid.value) return t('scout.product.previewChanged')
  return t('scout.product.previewReady', { count: fields.value.length })
})
const canReviewNext = computed(
  () =>
    !props.disabled &&
    status.value === 'ready' &&
    !tree.invalid.value &&
    (!cardReady.value || pendingFields.value.length > 0),
)
async function reviewNext() {
  if (!canReviewNext.value) return
  if (!cardReady.value) {
    target.value = 'record'
    recordSelector.value = form.value.recordSelector || ''
    manualRule.value = true
    await selectTab('field')
    editor.value?.querySelector('#scout-visual-record')?.focus()
  } else if (pendingFields.value.length) await editField(pendingFields.value[0].index)
}
const highlighted = computed(() => [
  ...new Set([...summary.value.nodeIds, ...(selectedNode.value ? [selectedNode.value] : [])]),
])

watch(valid, (value) => emit('valid', value), { immediate: true })
watch(presentation, (value) => emit('presentation', value), { immediate: true })
watch(
  candidateStamp,
  () => {
    if (applying) return
    invalidateCheck()
    result.value = null
    checkedStamp.value = ''
    if (snapshotReady.value) error.value = null
  },
  { flush: 'sync' },
)
watch(
  () => snapshot.value?.public_id,
  () => {
    result.value = null
    ancestors.value = []
    selectedNode.value = ''
    hoveredNode.value = ''
    cardNodeIds.value = []
    lastAccepted.value = null
    acceptedStamp.value = ''
    cardStamp.value = ''
    verifiedFields.clear()
  },
)
watch(
  () => form.value.recordMode,
  (mode) => {
    if (props.inspectorPrototype) cardNodeIds.value = []
    target.value = mode === 'cards' ? 'record' : 'field'
    draft.scope = mode === 'cards' ? 'record' : 'page'
    verifiedFields.clear()
    acceptedStamp.value = ''
    cardStamp.value = ''
    lastAccepted.value = null
  },
)
watch(target, () => {
  ancestors.value = []
  selectedNode.value = ''
  if (props.inspectorPrototype) {
    tree.clearSelection()
    hoveredNode.value = ''
  }
})

function body(nodeId) {
  if (target.value === 'record')
    return {
      record: nodeId ? { node_id: nodeId } : { kind: 'css', selector: recordSelector.value.trim() },
      field: null,
    }
  return {
    record: recordRule.value,
    field: {
      name: draft.name.trim(),
      scope: form.value.recordMode === 'page' ? 'page' : draft.scope,
      many: draft.many,
      ...(!nodeId && draft.value_source === 'image' ? { value_source: 'image' } : {}),
      ...(nodeId ? { node_id: nodeId } : { kind: 'css', selector: draft.selector.trim() }),
    },
  }
}
async function verify(nodeId) {
  if (props.inspectorPrototype && tree.invalid.value) return
  if (props.disabled || (target.value === 'field' && (!cardReady.value || !draft.name.trim())))
    return
  result.value = null
  checkedStamp.value = ''
  const response = await check({
    ...body(nodeId),
    ...(props.inspectorPrototype ? { tree_version: snapshot.value?.tree_version } : {}),
  })
  if (!response || (props.inspectorPrototype && tree.invalid.value)) return
  applying = true
  if (target.value === 'record') recordSelector.value = response.record?.selector || ''
  else Object.assign(draft, { value_source: undefined }, response.field)
  applying = false
  result.value = response
  checkedStamp.value = candidateStamp.value
}
function select(nodes) {
  ancestors.value = nodes
  chooseNode(nodes[0].id)
}
function chooseNode(id) {
  if (props.inspectorPrototype && tree.invalid.value) return
  error.value = null
  selectedNode.value = id
  manualRule.value = false
  if (props.inspectorPrototype) {
    result.value = null
    checkedStamp.value = ''
    invalidateCheck()
    tree.select(id)
  } else verify(id)
}
watch(
  () => tree.detail.value?.node.node_id,
  (id) => {
    if (id && id !== selectedNode.value) {
      result.value = null
      checkedStamp.value = ''
      invalidateCheck()
      selectedNode.value = id
    }
  },
)
watch(
  () => tree.invalid.value,
  (invalid) => {
    if (invalid) {
      selectedNode.value = ''
      hoveredNode.value = ''
      cardNodeIds.value = []
      cardStamp.value = ''
      verifiedFields.clear()
      result.value = null
      checkedStamp.value = ''
      acceptedStamp.value = ''
      invalidateCheck()
    }
  },
)
function cancelSelection() {
  selectedNode.value = ''
  hoveredNode.value = ''
  ancestors.value = []
  result.value = null
  checkedStamp.value = ''
  if (snapshotReady.value) error.value = null
  invalidateCheck()
  tree.clearSelection()
  editing.value = -1
  manualRule.value = false
  recordSelector.value = form.value.recordSelector || ''
  Object.assign(draft, {
    name: `field_${fields.value.length + 1}`,
    value_source: undefined,
    selector: '',
    many: false,
    scope: form.value.recordMode === 'cards' ? 'record' : 'page',
  })
}
function accept() {
  if (!acceptable.value) return
  const accepted = result.value
  if (target.value === 'record') {
    if (props.inspectorPrototype)
      cardNodeIds.value = accepted.rows.map((row) => row.node_id).filter(Boolean)
    verifiedFields.clear()
    form.value.recordKind = accepted.record.kind
    form.value.recordSelector = accepted.record.selector
    cardStamp.value = currentCardStamp.value
    acceptedStamp.value = ''
    manualRule.value = false
    target.value = 'field'
    draft.scope = 'record'
    return
  }
  const field = { ...result.value.field }
  const previousField = form.value.fields[editing.value]
  if (previousField?.conversion && !field.many && field.value_source !== 'image')
    field.conversion = previousField.conversion
  if (previousField?.typeSelectors) field.typeSelectors = previousField.typeSelectors
  const duplicate = form.value.fields.findIndex(
    (item, index) => item.name === field.name && index !== editing.value,
  )
  if (duplicate >= 0) return
  const index =
    editing.value >= 0
      ? editing.value
      : form.value.fields.findIndex((item) => !item.name && !item.selector)
  if (index >= 0) form.value.fields.splice(index, 1, field)
  else form.value.fields.push(field)
  form.value.fields = form.value.fields.filter((item) => item.name.trim() || item.selector.trim())
  // Every accepted existing rule must still be checked on this snapshot.
  verifiedFields.set(JSON.stringify(field), snapshot.value.public_id)
  if (
    fields.value.every(
      (item) =>
        verifiedFields.get(JSON.stringify(form.value.fields[item.index])) ===
        snapshot.value.public_id,
    )
  )
    acceptedStamp.value = visualPlanStamp(form.value)
  const matchIds = summary.value.nodeIds
  const nodeId = matchIds.includes(selectedNode.value) ? selectedNode.value : matchIds[0] || ''
  const detail = displayedNode.value?.node.node_id === nodeId ? displayedNode.value : null
  const selection = { name: field.name, nodeId, detail }
  cancelSelection()
  lastAccepted.value = selection
}
const duplicateName = computed(() =>
  form.value.fields.some(
    (field, index) =>
      field.name === draft.name.trim() && index !== editing.value && !!field.selector,
  ),
)
async function editField(index) {
  target.value = 'field'
  await nextTick()
  cancelSelection()
  editing.value = index
  Object.assign(draft, form.value.fields[index])
  manualRule.value = true
  await selectTab('field')
  editor.value?.querySelector('#scout-visual-name')?.focus()
}
function removeField(index) {
  editing.value = -1
  result.value = null
  form.value.fields.splice(index, 1)
  if (!form.value.fields.length)
    form.value.fields.push({ name: '', kind: 'css', selector: '', many: false, scope: 'page' })
  acceptedStamp.value =
    fields.value.length &&
    fields.value.every(
      (item) =>
        verifiedFields.get(JSON.stringify(form.value.fields[item.index])) ===
        snapshot.value?.public_id,
    )
      ? visualPlanStamp(form.value)
      : ''
}
async function addAnotherField() {
  lastAccepted.value = null
  cancelSelection()
  await selectTab('page')
}
</script>

<template>
  <div
    ref="editor"
    class="scout-visual-editor"
    :data-compact="compact"
    :style="{
      '--scout-tree-width': widths.tree + 'px',
      '--scout-field-width': widths.field + 'px',
    }"
    :class="{ 'scout-inspector-prototype': inspectorPrototype }"
    :data-prototype-tab="prototypeTab"
    :data-tree="showTree"
    :data-inspector="showInspector"
  >
    <div v-if="snapshotExpired" class="scout-visual-notice" data-tone="warning" role="alert">
      <TriangleAlert :size="18" aria-hidden="true" />
      <div>
        <strong>{{ design('expired') }}</strong>
        <p>{{ tree.invalid.value && tree.error.value ? t(`scout.inspector.${tree.error.value}`) : t('scout.visual.expired') }}</p>
      </div>
    </div>
    <FormCard class="scout-visual-source-card" :data-ready="snapshotReady">
      <div class="scout-visual-source">
        <h2 v-if="!snapshotReady" class="scout-visual-panel-title">{{ design('source') }}</h2>
        <Globe v-if="snapshotReady" class="scout-visual-source-icon" :size="18" aria-hidden="true" />
        <FormField
          class="scout-visual-url"
          :label="t('scout.url')"
          label-for="scout-visual-url"
          :hint="status === 'idle' ? design('sourceHint') : undefined"
        >
          <input
            id="scout-visual-url"
            ref="urlInput"
            v-model="form.url"
            class="form-control"
            type="url"
            :disabled="disabled"
            @keydown.enter.prevent="!disabled && status !== 'loading' && load()"
          />
        </FormField>
        <template v-if="snapshotReady">
          <span class="scout-visual-badge" data-tone="success">{{ design('ready') }}</span>
          <button
            type="button"
            class="btn btn-outline-secondary scout-visual-refresh"
            :disabled="disabled"
            @click="load"
          >
            <RefreshCw class="scout-visual-refresh-icon" :size="17" aria-hidden="true" />
            <span class="scout-visual-refresh-label">{{ design('refresh') }}</span>
          </button>
        </template>
        <div v-if="status === 'idle' && !snapshotExpired" class="scout-visual-source-details">
          <FormField :label="t('scout.jobTitle')" label-for="scout-visual-title">
            <input id="scout-visual-title" v-model="form.title" class="form-control" :disabled="disabled" />
          </FormField>
          <FormField :label="t('scout.product.proxy')" label-for="scout-visual-connection">
            <input id="scout-visual-connection" :value="design(form.proxyRevision ? 'withProxy' : 'direct')" class="form-control" readonly />
          </FormField>
        </div>
      </div>
    </FormCard>
    <template v-if="status === 'loading'">
      <div class="scout-visual-notice" data-tone="info" role="status">
        <Info :size="18" aria-hidden="true" />
        <div>
          <strong>{{ design('waitingSnapshot') }}</strong>
          <p>{{ design('loadingHint') }}</p>
        </div>
      </div>
      <FormCard class="scout-visual-placeholder" aria-busy="true">
        <h2 class="scout-visual-panel-title">{{ t('scout.visual.snapshotTitle') }}</h2>
        <div class="scout-visual-loading-surface" aria-hidden="true" />
      </FormCard>
    </template>
    <div v-else-if="error && !snapshotReady && !snapshotExpired" class="scout-visual-notice" data-tone="danger" role="alert">
      <TriangleAlert :size="18" aria-hidden="true" />
      <div>
        <strong>{{ design('loadError') }}</strong>
        <p>{{ t(`scout.visual.${error.key}`) }}</p>
        <p v-if="error.detail">{{ error.detail }}</p>
      </div>
    </div>
    <FormCard v-else-if="!snapshotReady && !snapshotExpired" class="scout-visual-placeholder">
      <div class="scout-visual-empty">
        <div class="scout-visual-empty-icon"><MousePointer2 :size="23" aria-hidden="true" /></div>
        <div>
          <h2>{{ design('empty') }}</h2>
          <p>{{ design('emptyHint') }}</p>
        </div>
      </div>
    </FormCard>
    <FormCard v-if="snapshotExpired && fields.length" class="scout-visual-placeholder">
      <h2 class="scout-visual-panel-title">{{ design('savedFields') }}</h2>
      <table class="scout-visual-expired-fields">
        <thead><tr><th>{{ t('scout.fieldName') }}</th><th>{{ t('scout.fieldSelector') }}</th><th>{{ design('state') }}</th></tr></thead>
        <tbody>
          <tr v-for="field in fields" :key="field.index">
            <td>{{ field.name }}</td>
            <td><code>{{ field.selector }}</code></td>
            <td><span class="scout-visual-badge" data-tone="warning">{{ t('scout.product.fieldNeedsCheck') }}</span></td>
          </tr>
        </tbody>
      </table>
    </FormCard>
    <div v-if="snapshotReady" class="scout-visual-layout">
      <template v-if="inspectorPrototype">
        <nav class="scout-prototype-tabs" role="tablist" :aria-label="t('scout.inspector.views')">
          <button
            v-for="(tab, index) in ['page', 'tree', 'field']"
            :key="tab"
            type="button"
            class="btn btn-outline-secondary"
            :id="`scout-tab-${tab}`"
            role="tab"
            :aria-selected="prototypeTab === tab"
            :aria-controls="`scout-panel-${tab}`"
            :tabindex="prototypeTab === tab ? 0 : -1"
            @keydown="tabKey($event, index)"
            @click="selectTab(tab)"
          >
            {{ tab === 'field' ? design('fields') : t(`scout.inspector.tabs.${tab}`) }}
          </button>
        </nav>
        <div class="scout-prototype-tools" :class="{ 'scout-prototype-tools--dock': showTree && showInspector }">
          <button
            type="button"
            class="btn btn-outline-secondary btn-sm"
            :aria-pressed="showTree"
            @click="showTree = !showTree"
          >
            {{ t('scout.inspector.structure') }}</button
          ><button
            type="button"
            class="btn btn-outline-secondary btn-sm"
            :aria-pressed="showInspector"
            @click="showInspector = !showInspector"
          >
            {{ t('scout.inspector.inspector') }}</button
          >
        </div>
        <div v-if="tree.error.value" class="alert alert-warning scout-prototype-error" role="alert">
          {{ t(`scout.inspector.${tree.error.value}`)
          }}<button type="button" class="btn btn-outline-secondary btn-sm" @click="load">
            {{ t('scout.visual.load') }}
          </button>
        </div>
        <ScoutPrototypeTree
          id="scout-panel-tree"
          :tabindex="-1"
          :role="compact ? 'tabpanel' : 'region'"
          aria-labelledby="scout-tab-tree"
          :tree="tree"
          :selected="displayedNodeId"
          @select="chooseNode"
        />
        <!-- Separator supports pointer dragging and keyboard arrows; the accessibility rule does not recognise this ARIA widget. -->
        <!-- eslint-disable-next-line vuejs-accessibility/no-static-element-interactions -->
        <div
          v-if="!compact && showTree"
          class="scout-resizer scout-resizer--tree"
          role="separator"
          tabindex="0"
          aria-orientation="vertical"
          :aria-label="t('scout.product.resizeTree')"
          :aria-valuenow="widths.tree"
          :aria-valuemin="210"
          :aria-valuemax="300"
          @pointerdown="resizeStart('tree', $event)"
          @keydown="resizeKey('tree', $event)"
        />
      </template>
      <section
        id="scout-panel-page"
        tabindex="-1"
        :role="inspectorPrototype && compact ? 'tabpanel' : 'region'"
        class="scout-visual-document"
        :aria-label="t('scout.visual.snapshotTitle')"
      >
        <header class="scout-visual-panel-header">
          <h2 class="scout-visual-panel-title">{{ t('scout.visual.snapshotTitle') }}</h2>
          <span class="scout-visual-badge" data-tone="neutral">{{ design('readOnly') }}</span>
        </header>
        <ScoutSnapshotFrame
          :html="snapshot.html"
          :node-ids="inspectorPrototype ? summary.nodeIds : highlighted"
          :prototype="inspectorPrototype"
          :selection-id="displayedNodeId"
          :card-ids="cardNodeIds"
          :hover-id="hoveredNode"
          @select="select"
          @candidate="hoveredNode = tree.invalid.value ? '' : $event"
        />
        <div v-if="inspectorPrototype" class="scout-visual-snapshot-meta">
          <span class="scout-visual-badge" :data-tone="displayedNodeId ? 'accent' : 'neutral'">
            {{ displayedNodeId ? design('selected') : design('pick') }}
          </span>
          <code v-if="displayedNodeId && displayedLabel">{{ displayedLabel }}</code>
          <span v-else>{{ design('snapshotHint') }}</span>
          <details v-if="cardNodeIds.length || summary.nodeIds.length" class="scout-visual-legend-details">
            <summary>{{ t('scout.inspector.legend') }}</summary>
            <div class="scout-prototype-legend" :aria-label="t('scout.inspector.legend')">
              <span v-for="role in ['hover', 'selected', 'card', 'matches']" :key="role" :data-role="role">
                {{ t(`scout.inspector.${role}`) }}
              </span>
            </div>
          </details>
        </div>
        <p class="visually-hidden">{{ t('scout.visual.keyboard') }}</p>
      </section>
      <!-- eslint-disable-next-line vuejs-accessibility/no-static-element-interactions -->
      <div
        v-if="inspectorPrototype && !compact && showInspector"
        class="scout-resizer scout-resizer--field"
        role="separator"
        tabindex="0"
        aria-orientation="vertical"
        :aria-label="t('scout.product.resizeFields')"
        :aria-valuenow="widths.field"
        :aria-valuemin="300"
        :aria-valuemax="400"
        @pointerdown="resizeStart('field', $event)"
        @keydown="resizeKey('field', $event)"
      />
      <section
        id="scout-panel-field"
        tabindex="-1"
        :role="inspectorPrototype && compact ? 'tabpanel' : 'region'"
        class="scout-visual-rules"
        :aria-label="t('scout.flow.rules')"
      >
        <header class="scout-visual-panel-header">
          <h2 class="scout-visual-panel-title">{{ showDraft && target === 'field' && draft.name.trim() ? draft.name : design('resultFields') }}</h2>
          <ImageIcon :size="16" aria-hidden="true" />
        </header>
        <div v-if="inspectorPrototype" class="scout-prototype-selection" aria-live="polite">
          <div v-if="!showDraft && !acceptedField" class="scout-visual-notice" data-tone="info">
            <Info :size="17" aria-hidden="true" />
            <div><strong>{{ design('pick') }}</strong><p>{{ design('pickHint') }}</p></div>
          </div>
          <div v-if="displayedNodeId || acceptedField" class="scout-visual-node-badges">
            <span class="scout-visual-badge" data-tone="accent">{{ acceptedField ? design('saved') : design('selected') }}</span>
            <span v-if="displayedImage" class="scout-visual-badge" data-tone="info">{{ t('scout.visual.image.field') }}</span>
          </div>
          <span class="scout-visual-node-label">{{ design('selectedNode') }}</span>
          <strong v-if="displayedPath" class="scout-prototype-selected-label">{{ displayedPath }}</strong>
          <p v-else class="small scout-muted mb-0">{{ design('noNode') }}</p>
          <p v-if="displayedNode?.node.text_preview && !displayedImage" class="small scout-muted mb-0">
            {{ displayedNode.node.text_preview }}
          </p>
          <details v-if="tree.detail.value && selectedNode" class="scout-visual-navigator">
            <summary>{{ design('navigation') }}</summary>
            <nav class="scout-visual-ancestors" :aria-label="t('scout.inspector.parents')">
              <button
                v-for="node in tree.detail.value.ancestors"
                :key="node.node_id"
                type="button"
                class="btn btn-outline-secondary btn-sm"
                @click="chooseNode(node.node_id)"
              >
                {{ nodeLabel(node) }}
              </button>
            </nav>
            <div class="scout-prototype-neighbours">
              <button
                v-for="direction in ['parent', 'child', 'previous', 'next']"
                :key="direction"
                type="button"
                class="btn btn-outline-secondary btn-sm"
                :disabled="
                  tree.loading.value ||
                  (direction === 'parent' && !tree.detail.value.node.parent_id) ||
                  (direction === 'child' && !tree.detail.value.node.children_count)
                "
                @click="tree.move(direction)"
              >
                {{ t(`scout.inspector.${direction}`) }}
              </button>
            </div>
          </details>
        </div>
        <fieldset :disabled="disabled">
          <legend class="visually-hidden">{{ t('scout.workspace.recordUnit') }}</legend>
          <details class="scout-visual-record-settings" :open="!inspectorPrototype || form.recordMode === 'cards'">
            <summary>{{ design('recordSettings') }}</summary>
            <div class="scout-radio-group">
            <label for="scout-visual-page"
              ><input
                id="scout-visual-page"
                v-model="form.recordMode"
                type="radio"
                value="page"
                name="scout-visual-unit"
              />{{ t('scout.flow.oneRecord') }}</label
            >
            <label for="scout-visual-cards"
              ><input
                id="scout-visual-cards"
                v-model="form.recordMode"
                type="radio"
                value="cards"
                name="scout-visual-unit"
              />{{ t('scout.visual.cards') }}</label
            >
            </div>
            <div v-if="form.recordMode === 'cards'" class="scout-visual-targets">
            <button
              v-if="form.recordMode === 'cards'"
              type="button"
              class="btn btn-outline-secondary"
              :aria-pressed="target === 'record'"
              @click="target = 'record'"
            >
              {{ t('scout.visual.card') }}
            </button>
            <button
              type="button"
              class="btn btn-outline-secondary"
              :aria-pressed="target === 'field'"
              :disabled="!cardReady"
              @click="target = 'field'"
            >
              {{ t('scout.visual.field') }}
            </button>
            </div>
            <button v-if="inspectorPrototype && !showDraft && target === 'field'" type="button" class="btn btn-outline-secondary btn-sm" @click="manualRule = true">
              {{ design('selector') }}
            </button>
            <button type="button" class="btn btn-outline-secondary btn-sm" @click="emit('manual')">
              {{ t('scout.workspace.useManual') }}
            </button>
          </details>
          <div v-if="ancestors.length && !inspectorPrototype" class="scout-visual-ancestors">
            <span class="small">{{ t('scout.visual.ancestors') }}</span>
            <button
              v-for="node in ancestors"
              :key="node.id"
              type="button"
              class="btn btn-outline-secondary btn-sm"
              :title="node.text"
              :aria-pressed="selectedNode === node.id"
              @click="chooseNode(node.id)"
            >
              {{ node.tag }}
            </button>
          </div>
          <template v-if="target === 'record'">
            <label class="scout-input-label" for="scout-visual-record"
              >{{ t('scout.visual.cardCss')
              }}<input
                id="scout-visual-record"
                v-model="recordSelector"
                class="form-control"
                @input="manualRule = true"
            /></label>
          </template>
          <template v-else-if="showDraft || acceptedField">
            <div class="scout-visual-field-form">
              <label class="scout-input-label" for="scout-visual-name">
                {{ t('scout.fieldName') }}
                <input v-if="acceptedField" id="scout-visual-name" :value="acceptedField.name" class="form-control" readonly />
                <input v-else id="scout-visual-name" v-model="draft.name" class="form-control" />
              </label>
              <label class="scout-input-label" for="scout-visual-scope">
                {{ design('scope') }}
                <select
                  v-if="form.recordMode === 'cards' && !acceptedField"
                  id="scout-visual-scope"
                  v-model="draft.scope"
                  class="form-select"
                >
                <option value="record">{{ t('scout.scopeRecord') }}</option>
                <option value="page">{{ t('scout.scopePage') }}</option>
                </select>
                <input v-else id="scout-visual-scope" class="form-control" :value="t(acceptedField?.scope === 'record' ? 'scout.scopeRecord' : 'scout.scopePage')" readonly />
              </label>
              <details class="scout-visual-selector" open>
                <summary>{{ design('selector') }}<ChevronDown :size="15" aria-hidden="true" /></summary>
                <label class="visually-hidden" for="scout-visual-css">{{ t('scout.visual.fieldCss') }}</label>
                <input v-if="acceptedField" id="scout-visual-css" :value="acceptedField.selector" class="form-control" readonly />
                <input
                  v-else
                  id="scout-visual-css"
                  v-model="draft.selector"
                  class="form-control"
                  :placeholder="displayedLabel"
                  :aria-invalid="error ? 'true' : undefined"
                  :aria-describedby="error ? 'scout-visual-css-error' : undefined"
                  @input="manualRule = true"
                />
              </details>
              <details v-if="!acceptedField" class="scout-visual-field-options">
                <summary>{{ t('scout.fieldMany') }}</summary>
                <ToggleSwitch id="scout-visual-many" v-model="draft.many" :label="t('scout.fieldMany')" />
              </details>
              <p v-if="!checked && !acceptedField && !error" class="small scout-muted mb-0">
                {{ design('selectorHint') }}
              </p>
            </div>
            <p v-if="!acceptedField && duplicateName" class="scout-warning small" role="alert">
              {{ t('scout.visual.duplicate') }}
            </p>
          </template>
          <div v-if="error" id="scout-visual-css-error" class="scout-visual-notice" data-tone="danger" role="alert">
            <TriangleAlert :size="18" aria-hidden="true" />
            <div>
              <strong>{{ design('ruleNotApplied') }}</strong>
              <p>{{ t(`scout.visual.${error.key}`) }}</p>
              <p v-if="error.detail">{{ error.detail }}</p>
            </div>
          </div>
          <button
            v-if="!inspectorPrototype"
            type="button"
            class="btn btn-outline-secondary mt-3"
            :disabled="
              checking ||
              (target === 'field'
                ? !cardReady || !draft.name.trim() || !draft.selector.trim()
                : !recordSelector.trim())
            "
            @click="verify()"
          >
            {{ checking ? t('scout.visual.checking') : t('scout.visual.check') }}
          </button>
          <div v-if="checked" class="scout-visual-check" :data-acceptable="acceptable" aria-live="polite">
            <div class="scout-visual-notice" :data-tone="acceptable ? 'success' : 'warning'">
              <Check v-if="acceptable" :size="18" aria-hidden="true" />
              <TriangleAlert v-else :size="18" aria-hidden="true" />
              <div>
                <strong>{{ target === 'field' ? design('matches', summary.matches) : t('scout.visual.rowCount', { count: result.row_count }) }}</strong>
                <p v-if="result.field?.value_source === 'image'">{{ t('scout.visual.image.fieldHint') }}</p>
                <p v-if="!acceptable">{{ t('scout.visual.notFound') }}</p>
              </div>
            </div>
            <p v-if="target === 'field' && summary.missing" class="scout-warning small">
              {{ t('scout.visual.missing', { count: summary.missing }) }}
            </p>
            <p
              v-if="target === 'field' && !draft.many && summary.multiple"
              class="alert alert-warning"
            >
              {{ t('scout.visual.multiple') }}
            </p>
            <ol v-if="target === 'field' && result.field?.value_source !== 'image'" class="scout-visual-samples">
              <li v-for="(row, index) in result.rows" :key="index">
                {{
                  row.match_count
                    ? displayValue(
                        row.value,
                        t(
                          inspectorPrototype ? 'scout.inspector.emptyValue' : 'scout.flow.notFound',
                        ),
                      )
                    : t('scout.visual.notFound')
                }}
              </li>
            </ol>
            <p v-if="inspectorPrototype && result.field?.value_source !== 'image'" class="scout-muted small">
              {{
                t('scout.inspector.sampleHint', {
                  shown: result.rows.length,
                  total: result.row_count,
                })
              }}
            </p>
            <details v-if="result.field?.value_source === 'image'" class="scout-visual-field-options">
              <summary>{{ design('examples') }}</summary>
              <ol class="scout-visual-samples">
                <li v-for="(row, index) in result.rows" :key="index">
                  {{ displayValue(row.value, t('scout.visual.notFound')) }}
                </li>
              </ol>
            </details>
            <button
              v-if="!inspectorPrototype"
              type="button"
              class="btn btn-primary"
              :disabled="!acceptable || (target === 'field' && duplicateName)"
              @click="accept"
            >
              {{ t(target === 'record' ? 'scout.visual.acceptCard' : 'scout.visual.acceptField') }}
            </button>
          </div>
          <h3 v-if="fields.length && !acceptedField" class="scout-visual-panel-title scout-visual-fields-title">{{ t('scout.visual.acceptedFields') }}</h3>
          <p v-if="!fields.length && !showDraft" class="scout-muted small mb-0">{{ design('fieldHint') }}</p>
          <ul v-if="fields.length" class="scout-visual-fields">
            <li v-for="field in fields" :key="field.index">
              <strong :class="{ 'visually-hidden': field.index === acceptedField?.index }">
                {{ field.name }}<template v-if="field.value_source === 'image'"> · {{ t('scout.visual.image.field') }}</template>
              </strong>
              <code :class="{ 'visually-hidden': field.index === acceptedField?.index }">{{ field.selector }}</code>
              <div v-if="field.index === acceptedField?.index" class="scout-visual-saved-meta">
                <span class="scout-visual-badge" data-tone="success">{{ design('fieldSaved') }}</span>
                <button type="button" class="btn btn-link btn-sm" @click="editField(field.index)">{{ t('scout.visual.edit') }}</button>
                <button type="button" class="btn btn-outline-secondary btn-sm" @click="removeField(field.index)">{{ t('scout.visual.remove') }}</button>
              </div>
              <p v-if="field.index === acceptedField?.index && field.value_source === 'image'" class="small scout-muted mb-0">{{ t('scout.visual.image.fieldHint') }}</p>
              <small v-if="inspectorPrototype"
                class="scout-field-verification"
                :class="{ 'visually-hidden': field.index === acceptedField?.index }"
                :data-verified="fieldVerified(field)"
                >{{
                  t(
                    fieldVerified(field)
                      ? 'scout.product.fieldVerified'
                      : 'scout.product.fieldNeedsCheck',
                  )
                }}</small
              >
              <div v-if="field.index !== acceptedField?.index">
                <button
                  type="button"
                  class="btn btn-outline-secondary btn-sm"
                  @click="editField(field.index)"
                >
                  {{ t('scout.visual.edit') }}</button
                ><button
                  type="button"
                  class="btn btn-outline-secondary btn-sm"
                  @click="removeField(field.index)"
                >
                  {{ t('scout.visual.remove') }}
                </button>
              </div>
            </li>
          </ul>
          <ScoutImageDownload
            v-if="canDownloadImage"
            :key="`${snapshot.public_id}/${snapshot.tree_version}/${displayedNodeId}`"
            :endpoint="scoutEndpoints.visualSnapshotImage(snapshot.public_id)"
            :params="{ tree_version: snapshot.tree_version, node_id: displayedNodeId }"
            :filename-stem="`scout-${displayedNodeId}`"
          />
        </fieldset>
      </section>
    </div>
    <div class="scout-inspector-actions" aria-live="polite">
      <div class="scout-visual-action-hint" :class="{ 'visually-hidden': !canReviewNext }">
        <p id="scout-preview-requirements" class="small scout-muted mb-0">{{ previewHint }}</p>
        <button
          v-if="canReviewNext"
          type="button"
          class="btn btn-outline-secondary btn-sm mt-2 scout-review-next"
          @click="reviewNext"
        >
          {{ t(!cardReady ? 'scout.product.reviewCard' : 'scout.product.reviewNext') }}
        </button>
      </div>
      <div class="scout-inspector-action-buttons">
        <button
          v-if="status !== 'loading' && (inspectorPrototype || !snapshotReady || valid)"
          type="button"
          class="btn btn-primary"
          :disabled="inspectorPrototype || !snapshotReady ? primaryDisabled : disabled"
          aria-describedby="scout-preview-requirements"
          @click="inspectorPrototype || !snapshotReady ? primaryAction() : emit('preview')"
        >
          {{ inspectorPrototype || !snapshotReady ? primaryLabel : t('scout.workspace.preparePreview') }}
        </button>
        <button v-if="status === 'loading'" type="button" class="btn btn-outline-secondary" @click="cancelLoading">
          {{ design('cancelLoading') }}
        </button>
        <button v-else-if="snapshotExpired || status === 'failed' || !inspectorPrototype" type="button" class="btn btn-outline-secondary" :disabled="disabled" @click="emit('manual')">
          {{ t('scout.workspace.useManual') }}
        </button>
        <button v-else-if="!snapshotReady" type="button" class="btn btn-outline-secondary" :disabled="disabled" @click="emit('back')">
          {{ design('backToMode') }}
        </button>
        <button v-else-if="canCancel" type="button" class="btn btn-outline-secondary" :disabled="disabled" @click="cancelSelection">
          {{ t('scout.inspector.cancel') }}
        </button>
        <button v-else-if="valid" type="button" class="btn btn-outline-secondary" :disabled="disabled" @click="addAnotherField">
          {{ design(compact ? 'backToSnapshot' : 'moreField') }}
        </button>
        <button v-else type="button" class="btn btn-outline-secondary" :disabled="disabled" @click="urlInput?.focus()">
          {{ design('backToSource') }}
        </button>
      </div>
    </div>
  </div>
</template>
