<script setup>
import { computed, nextTick } from 'vue'
import { ChevronDown, ChevronRight, ListTree, Search } from '@lucide/vue'
import { useAppI18n } from '@/i18n/useAppI18n.js'
import { nodeLabel } from './useInspectorTree.js'
const props = defineProps({ tree: Object, selected: String })
const emit = defineEmits(['select'])
const { t } = useAppI18n()
function backToSelection() {
  props.tree.setQuery('')
  emit('select', props.selected)
}
const focusId = computed(() =>
  props.tree.visible.value.some((node) => node.node_id === props.selected)
    ? props.selected
    : props.tree.visible.value[0]?.node_id,
)
async function key(event, node) {
  const root = event.currentTarget.closest('[role=tree]')
  const tree = props.tree
  const items = tree.visible.value
  const index = items.findIndex((item) => item.node_id === node.node_id)
  let next
  if (event.key === 'ArrowDown') next = items[index + 1]
  if (event.key === 'ArrowUp') next = items[index - 1]
  if (event.key === 'ArrowRight') {
    if (node.children_count && !tree.expanded.value.has(node.node_id))
      await tree.toggle(node.node_id)
    else next = items[index + 1]
  }
  if (event.key === 'ArrowLeft') {
    if (tree.expanded.value.has(node.node_id)) await tree.toggle(node.node_id)
    else next = tree.nodes.value[node.parent_id]
  }
  if (event.key === 'Home') next = items[0]
  if (event.key === 'End') next = items.at(-1)
  if (event.key === 'Enter' || event.key === ' ') emit('select', node.node_id)
  if (
    ['ArrowDown', 'ArrowUp', 'ArrowLeft', 'ArrowRight', 'Home', 'End', 'Enter', ' '].includes(
      event.key,
    )
  )
    event.preventDefault()
  if (next) {
    await nextTick()
    root.querySelector(`[data-node="${next.node_id}"]`)?.focus()
  }
}
</script>
<template>
  <section class="scout-prototype-tree" :aria-label="t('scout.inspector.structure')">
    <header class="scout-visual-panel-header">
      <h2 class="scout-visual-panel-title">{{ t('scout.inspector.structure') }}</h2>
      <ListTree :size="16" aria-hidden="true" />
    </header>
    <form class="scout-prototype-search" @submit.prevent="tree.search()">
      <label class="visually-hidden" for="scout-dom-search">{{ t('scout.inspector.searchLabel') }}</label>
      <button
        class="btn scout-prototype-search-button"
        type="submit"
        :disabled="tree.searching.value || tree.invalid.value"
        :aria-label="t('scout.inspector.search')"
      >
        <Search :size="16" aria-hidden="true" />
      </button>
      <input
        id="scout-dom-search"
        :value="tree.query.value"
        :placeholder="t('scout.visualDesign.searchPlaceholder')"
        @input="tree.setQuery($event.target.value)"
        class="form-control"
        type="search"
        maxlength="200"
      />
    </form>
    <p v-if="tree.loading.value || tree.searching.value" role="status">
      {{ t('scout.inspector.loading') }}
    </p>
    <div v-if="tree.searchResult.value" class="scout-prototype-search-results">
      <p class="small" role="status">
        {{
          t('scout.inspector.searchCount', {
            count: tree.searchResult.value.total_count,
            shown: tree.searchResult.value.items.length,
          })
        }}
      </p>
      <button
        v-for="node in tree.searchResult.value.items"
        :key="node.node_id"
        type="button"
        class="scout-prototype-node"
        :aria-pressed="selected === node.node_id"
        @click="emit('select', node.node_id)"
      >
        {{ nodeLabel(node) }}<small>{{ node.text_preview || t('scout.inspector.noText') }}</small>
      </button>
      <div class="scout-prototype-paging">
        <button
          type="button"
          class="btn btn-outline-secondary btn-sm"
          :disabled="!tree.searchResult.value.previous_cursor"
          @click="tree.search(tree.searchResult.value.previous_cursor)"
        >
          {{ t('scout.inspector.previous') }}</button
        ><button
          type="button"
          class="btn btn-outline-secondary btn-sm"
          :disabled="!tree.searchResult.value.next_cursor"
          @click="tree.search(tree.searchResult.value.next_cursor)"
        >
          {{ t('scout.inspector.next') }}
        </button>
      </div>
    </div>
    <ul class="scout-prototype-nodes" role="tree" :aria-label="t('scout.inspector.structure')">
      <!-- eslint-disable-next-line vuejs-accessibility/interactive-supports-focus -- Roving tabindex keeps one visible tree node in the tab order; keyboard navigation is browser-tested. -->
      <li
        v-for="node in tree.visible.value"
        :key="node.node_id"
        role="treeitem"
        :tabindex="focusId === node.node_id ? 0 : -1"
        :data-node="node.node_id"
        :data-depth="Math.min(node.depth, 4)"
        :aria-level="node.depth + 1"
        :aria-selected="selected === node.node_id"
        :aria-expanded="node.children_count ? tree.expanded.value.has(node.node_id) : undefined"
        @click="emit('select', node.node_id)"
        @keydown="key($event, node)"
      >
        <button
          v-if="node.children_count"
          type="button"
          tabindex="-1"
          class="scout-prototype-expand"
          :aria-label="
            t(
              tree.expanded.value.has(node.node_id)
                ? 'scout.inspector.collapse'
                : 'scout.inspector.expand',
              { node: nodeLabel(node) },
            )
          "
          @click.stop="tree.toggle(node.node_id)"
        >
          <ChevronDown v-if="tree.expanded.value.has(node.node_id)" :size="13" aria-hidden="true" />
          <ChevronRight v-else :size="13" aria-hidden="true" />
        </button>
        <span class="scout-prototype-node-text">
          <code>{{ nodeLabel(node) }}</code>
          <small v-if="node.text_preview">{{ node.text_preview }}</small>
        </span>
      </li>
    </ul>
    <div
      v-for="(branch, id) in tree.branches.value"
      v-show="id === 'root' || tree.expanded.value.has(id)"
      :key="id"
      class="scout-prototype-paging"
    >
      <template v-if="branch.previous_cursor || branch.next_cursor"
        ><small>{{
          t('scout.inspector.branchPage', {
            node: id === 'root' ? t('scout.inspector.roots') : nodeLabel(tree.nodes.value[id]),
            count: branch.total_count,
          })
        }}</small
        ><button
          type="button"
          class="btn btn-outline-secondary btn-sm"
          :disabled="!branch.previous_cursor"
          @click="tree.branch(id === 'root' ? null : id, branch.previous_cursor)"
        >
          {{ t('scout.inspector.previous') }}</button
        ><button
          type="button"
          class="btn btn-outline-secondary btn-sm"
          :disabled="!branch.next_cursor"
          @click="tree.branch(id === 'root' ? null : id, branch.next_cursor)"
        >
          {{ t('scout.inspector.next') }}
        </button></template
      >
    </div>
    <div class="scout-prototype-tree-footer">
      <button v-if="selected" type="button" class="btn btn-link btn-sm" @click="backToSelection">
        {{ t('scout.inspector.backToSelection') }}
      </button>
      <small v-else>{{ t('scout.inspector.treeCount', { count: tree.nodeCount.value }) }}</small>
    </div>
  </section>
</template>
