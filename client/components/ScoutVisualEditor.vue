<script setup>
import { computed, reactive, ref, watch } from 'vue'
import FormCard from '@/components/FormCard.vue'
import FormField from '@/components/FormField.vue'
import SpinnerLoading from '@/components/SpinnerLoading.vue'
import ToggleSwitch from '@/components/ToggleSwitch.vue'
import { useAppI18n } from '@/i18n/useAppI18n.js'
import { useVisualSnapshot } from '../js/useVisualSnapshot.js'
import { matchSummary, visualPlanStamp } from '../js/visualState.js'
import { displayValue } from '../js/recordTable.js'
import ScoutSnapshotFrame from './ScoutSnapshotFrame.vue'
import ScoutPrototypeTree from '../prototypes/ScoutPrototypeTree.vue'
import { useInspectorTree, nodeLabel } from '../prototypes/useInspectorTree.js'

const form = defineModel({ type: Object, required: true })
const props = defineProps({ disabled: Boolean, inspectorPrototype: Boolean })
const emit = defineEmits(['valid', 'preview', 'manual'])
const { t } = useAppI18n()
const active = ref(true)
const url = computed(() => form.value.url)
const { snapshot, status, error, checking, load, check, invalidateCheck } = useVisualSnapshot(
  url,
  active,
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
const recordSelector = ref(form.value.recordSelector || '')
const draft = reactive({ name: 'field_1', selector: '', many: false, scope: 'page' })
const editing = ref(-1)
const result = ref(null)
const checkedStamp = ref('')
const acceptedStamp = ref('')
const cardStamp = ref('')
const verifiedFields = new Map()
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
const highlighted = computed(() => [
  ...new Set([...summary.value.nodeIds, ...(selectedNode.value ? [selectedNode.value] : [])]),
])

watch(valid, (value) => emit('valid', value), { immediate: true })
watch(
  candidateStamp,
  () => {
    if (applying) return
    invalidateCheck()
    result.value = null
    checkedStamp.value = ''
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
  else Object.assign(draft, response.field)
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
  selectedNode.value = id
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
  invalidateCheck()
  tree.clearSelection()
  if (editing.value >= 0) Object.assign(draft, form.value.fields[editing.value])
  else draft.selector = ''
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
    target.value = 'field'
    draft.scope = 'record'
    return
  }
  const field = { ...result.value.field }
  const conversion = form.value.fields[editing.value]?.conversion
  if (conversion && !field.many) field.conversion = conversion
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
  result.value = null
  checkedStamp.value = ''
  ancestors.value = []
  selectedNode.value = ''
  editing.value = -1
  Object.assign(draft, {
    name: `field_${fields.value.length + 1}`,
    selector: '',
    many: false,
    scope: form.value.recordMode === 'cards' ? 'record' : 'page',
  })
}
const duplicateName = computed(() =>
  form.value.fields.some(
    (field, index) =>
      field.name === draft.name.trim() && index !== editing.value && !!field.selector,
  ),
)
function editField(index) {
  target.value = 'field'
  editing.value = index
  Object.assign(draft, form.value.fields[index])
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
</script>

<template>
  <div
    class="scout-visual-editor"
    :class="{ 'scout-inspector-prototype': inspectorPrototype }"
    :data-prototype-tab="prototypeTab"
    :data-tree="showTree"
    :data-inspector="showInspector"
  >
    <FormCard>
      <FormField
        :label="t('scout.url')"
        label-for="scout-visual-url"
        :hint="t('scout.visual.sourceHint')"
      >
        <div class="scout-visual-source">
          <input
            id="scout-visual-url"
            v-model="form.url"
            class="form-control"
            type="url"
            :disabled="disabled"
          /><button
            type="button"
            class="btn btn-primary"
            :disabled="disabled || status === 'loading'"
            @click="load"
          >
            {{ t('scout.visual.load') }}
          </button>
        </div>
      </FormField>
    </FormCard>
    <p v-if="status === 'loading'" class="scout-visual-status" role="status">
      <SpinnerLoading />{{ t('scout.visual.loading') }}
    </p>
    <div v-if="error" class="alert alert-warning mt-3" role="alert">
      {{ t(`scout.visual.${error.key}`) }}
      <p v-if="error.detail" class="small mb-0">{{ error.detail }}</p>
    </div>
    <div v-if="status === 'ready'" class="scout-visual-layout">
      <template v-if="inspectorPrototype">
        <nav class="scout-prototype-tabs" :aria-label="t('scout.inspector.views')">
          <button
            v-for="tab in ['page', 'tree', 'field']"
            :key="tab"
            type="button"
            class="btn btn-outline-secondary"
            :aria-pressed="prototypeTab === tab"
            @click="prototypeTab = tab"
          >
            {{ t(`scout.inspector.tabs.${tab}`) }}
          </button>
        </nav>
        <div class="scout-prototype-tools">
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
          ><span class="small scout-muted">{{ t('scout.inspector.draftHint') }}</span>
        </div>
        <div v-if="tree.error.value" class="alert alert-warning scout-prototype-error" role="alert">
          {{ t(`scout.inspector.${tree.error.value}`)
          }}<button type="button" class="btn btn-outline-secondary btn-sm" @click="load">
            {{ t('scout.visual.load') }}
          </button>
        </div>
        <ScoutPrototypeTree :tree="tree" :selected="selectedNode" @select="chooseNode" />
      </template>
      <section class="scout-visual-document" :aria-label="t('scout.visual.snapshotTitle')">
        <h2 class="h6">{{ t('scout.visual.snapshotTitle') }}</h2>
        <p class="scout-muted small">{{ t('scout.visual.frameHint') }}</p>
        <ScoutSnapshotFrame
          :html="snapshot.html"
          :node-ids="inspectorPrototype ? summary.nodeIds : highlighted"
          :prototype="inspectorPrototype"
          :selection-id="selectedNode"
          :card-ids="cardNodeIds"
          :hover-id="hoveredNode"
          @select="select"
          @candidate="hoveredNode = tree.invalid.value ? '' : $event"
        />
        <div
          v-if="inspectorPrototype"
          class="scout-prototype-legend"
          :aria-label="t('scout.inspector.legend')"
        >
          <span
            v-for="role in ['hover', 'selected', 'card', 'matches']"
            :key="role"
            :data-role="role"
            >{{ t(`scout.inspector.${role}`) }}</span
          >
        </div>
        <p class="scout-muted small mt-2">{{ t('scout.visual.keyboard') }}</p>
      </section>
      <section class="scout-visual-rules" :aria-label="t('scout.flow.rules')">
        <div v-if="inspectorPrototype" class="scout-prototype-selection" aria-live="polite">
          <h2 class="h6">{{ t('scout.inspector.inspector') }}</h2>
          <p v-if="!selectedNode" class="scout-muted small">
            {{ t('scout.inspector.selectHint') }}
          </p>
          <template v-if="tree.detail.value">
            <strong class="scout-prototype-selected-label">{{
              nodeLabel(tree.detail.value.node)
            }}</strong>
            <p class="small">
              {{ tree.detail.value.node.text_preview || t('scout.inspector.noText') }}
            </p>
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
          </template>
          <button
            v-if="selectedNode"
            type="button"
            class="btn btn-outline-secondary btn-sm mt-2"
            @click="cancelSelection"
          >
            {{ t('scout.inspector.cancel') }}
          </button>
        </div>
        <fieldset :disabled="disabled">
          <legend class="h6">{{ t('scout.workspace.recordUnit') }}</legend>
          <div class="scout-radio-group mb-3">
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
          <div class="scout-visual-targets">
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
          <p class="scout-muted small mt-2">
            {{ t(target === 'record' ? 'scout.visual.pickCard' : 'scout.visual.pickField') }}
          </p>
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
              }}<input id="scout-visual-record" v-model="recordSelector" class="form-control"
            /></label>
          </template>
          <template v-else>
            <label class="scout-input-label" for="scout-visual-name"
              >{{ t('scout.fieldName')
              }}<input id="scout-visual-name" v-model="draft.name" class="form-control"
            /></label>
            <label class="scout-input-label mt-2" for="scout-visual-css"
              >{{ t('scout.visual.fieldCss')
              }}<input id="scout-visual-css" v-model="draft.selector" class="form-control"
            /></label>
            <label
              v-if="form.recordMode === 'cards'"
              class="scout-input-label mt-2"
              for="scout-visual-scope"
              >{{ t('scout.fieldScope')
              }}<select id="scout-visual-scope" v-model="draft.scope" class="form-select">
                <option value="record">{{ t('scout.scopeRecord') }}</option>
                <option value="page">{{ t('scout.scopePage') }}</option>
              </select></label
            >
            <div class="my-3">
              <ToggleSwitch
                id="scout-visual-many"
                v-model="draft.many"
                :label="t('scout.fieldMany')"
              />
            </div>
            <p v-if="duplicateName" class="scout-warning small" role="alert">
              {{ t('scout.visual.duplicate') }}
            </p>
          </template>
          <button
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
          <button
            v-if="inspectorPrototype"
            type="button"
            class="btn btn-primary mt-3"
            :disabled="
              !selectedNode ||
              checking ||
              tree.invalid.value ||
              tree.loading.value ||
              (target === 'field' && !cardReady)
            "
            @click="verify(selectedNode)"
          >
            {{ t('scout.inspector.propose') }}
          </button>
          <div v-if="checked" class="scout-visual-check" aria-live="polite">
            <p class="fw-semibold">{{ t('scout.visual.rowCount', { count: result.row_count }) }}</p>
            <p v-if="target === 'field'">
              {{ t('scout.visual.matches', { count: summary.matches }) }}
            </p>
            <p v-if="!acceptable" class="scout-warning" role="status">
              {{ t('scout.visual.notFound') }}
            </p>
            <p v-if="target === 'field' && summary.missing" class="scout-warning small">
              {{ t('scout.visual.missing', { count: summary.missing }) }}
            </p>
            <p
              v-if="target === 'field' && !draft.many && summary.multiple"
              class="alert alert-warning"
            >
              {{ t('scout.visual.multiple') }}
            </p>
            <ol v-if="target === 'field'" class="scout-visual-samples">
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
            <p v-if="inspectorPrototype" class="scout-muted small">
              {{
                t('scout.inspector.sampleHint', {
                  shown: result.rows.length,
                  total: result.row_count,
                })
              }}
            </p>
            <button
              type="button"
              class="btn btn-primary"
              :disabled="!acceptable || (target === 'field' && duplicateName)"
              @click="accept"
            >
              {{ t(target === 'record' ? 'scout.visual.acceptCard' : 'scout.visual.acceptField') }}
            </button>
          </div>
          <h3 class="h6 mt-4">{{ t('scout.visual.acceptedFields') }}</h3>
          <p v-if="!fields.length" class="scout-muted small">{{ t('scout.visual.noFields') }}</p>
          <ul class="scout-visual-fields">
            <li v-for="field in fields" :key="field.index">
              <strong>{{ field.name }}</strong
              ><code>{{ field.selector }}</code>
              <div>
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
        </fieldset>
      </section>
    </div>
    <div class="scout-actions mt-3">
      <button
        type="button"
        class="btn btn-outline-secondary"
        :disabled="disabled"
        @click="emit('manual')"
      >
        {{ t('scout.workspace.useManual') }}</button
      ><button
        type="button"
        class="btn btn-primary"
        :disabled="disabled || !valid"
        @click="emit('preview')"
      >
        {{ t('scout.workspace.preparePreview') }}
      </button>
    </div>
  </div>
</template>
