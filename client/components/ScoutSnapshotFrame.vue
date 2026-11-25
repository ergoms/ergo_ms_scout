<script setup>
import { nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { useAppI18n } from '@/i18n/useAppI18n.js'

const props = defineProps({
  html: String,
  nodeIds: { type: Array, default: () => [] },
  prototype: Boolean,
  selectionId: String,
  hoverId: String,
  cardIds: { type: Array, default: () => [] },
})
const emit = defineEmits(['select', 'candidate'])
const { t } = useAppI18n()
const frame = ref(null)
const rectangles = ref([])
let cleanup = () => {}
let observer
let themeObserver
let animation = 0
function update() {
  cancelAnimationFrame(animation)
  animation = requestAnimationFrame(() => {
    const document = frame.value?.contentDocument
    const layers = props.prototype
      ? [
          ...props.cardIds.map((id) => ({ id, role: 'card' })),
          ...props.nodeIds.map((id) => ({ id, role: 'match' })),
          ...(props.selectionId ? [{ id: props.selectionId, role: 'selected' }] : []),
          ...(props.hoverId ? [{ id: props.hoverId, role: 'hover' }] : []),
        ]
      : props.nodeIds.map((id) => ({ id, role: 'match' }))
    rectangles.value = layers.flatMap(({ id, role }) => {
      if (!/^n\d+$/.test(id)) return []
      const element = document?.querySelector(`[data-scout-node="${id}"]`)
      if (!element) return []
      const box = element.getBoundingClientRect()
      return [{ id, role, x: box.x, y: box.y, width: box.width, height: box.height }]
    })
  })
}
function attach() {
  cleanup()
  const document = frame.value?.contentDocument
  if (!document?.body) return
  // Keep image selection visible without changing the geometry of loaded photos.
  if ('adoptedStyleSheets' in document && document.querySelector('[data-scout-image]')) {
    const sheet = new frame.value.contentWindow.CSSStyleSheet()
    sheet.replaceSync(`
      [data-scout-image=""] {
        display: inline-block;
        padding: .25em .5em;
        border: 1px dashed currentColor;
        border-radius: .25em;
      }
      [data-scout-image] { cursor: pointer; }
      [data-scout-image]:focus-visible { outline: 2px solid Highlight; }
    `)
    document.adoptedStyleSheets = [...document.adoptedStyleSheets, sheet]
  }
  // Scripts remain disabled by sandbox. Parent listeners only read server-issued node IDs.
  const scheme = document.createElement('meta')
  scheme.name = 'color-scheme'
  document.head.append(scheme)
  const syncTheme = () => {
    scheme.content = window.document.documentElement.dataset.bsTheme === 'dark' ? 'dark' : 'light'
    update()
  }
  syncTheme()
  themeObserver?.disconnect()
  themeObserver = new MutationObserver(syncTheme)
  themeObserver.observe(window.document.documentElement, {
    attributes: true,
    attributeFilter: ['data-bs-theme'],
  })
  let focused = document.querySelector('[data-scout-node]')
  if (focused) focused.tabIndex = 0
  function select(element) {
    const ancestors = []
    for (let node = element; node?.hasAttribute('data-scout-node'); node = node.parentElement) {
      ancestors.push({
        id: node.getAttribute('data-scout-node'),
        tag: node.tagName.toLowerCase(),
        text: (node.textContent || '').trim().slice(0, 70),
      })
    }
    if (ancestors.length) emit('select', ancestors)
  }
  function focus(element) {
    if (!element?.hasAttribute('data-scout-node')) return
    if (focused) focused.tabIndex = -1
    focused = element
    focused.tabIndex = 0
    focused.focus()
  }
  function hitNode(event) {
    const element = event.target.closest('[data-scout-node]')
    if (
      !element ||
      !Number.isFinite(event.clientX) ||
      element.textContent.trim() ||
      element.matches(
        'a, button, img, input, label, option, select, summary, textarea, [role="button"], [role="img"], [role="link"]',
      )
    ) return element

    // An empty decoration can sit over a photo while sharing its container.
    return document.elementsFromPoint(event.clientX, event.clientY).find((hit) =>
      hit.parentElement === element.parentElement &&
      hit.matches('img[data-scout-node][data-scout-image="preview"]') &&
      hit.complete &&
      hit.naturalWidth > 0,
    ) || element
  }
  const click = (event) => {
    event.preventDefault()
    event.stopPropagation()
    const element = hitNode(event)
    if (element) {
      focus(element)
      select(element)
    }
  }
  const key = (event) => {
    const element = event.target.closest('[data-scout-node]')
    if (!element) return
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault()
      select(element)
    }
    const next = {
      ArrowUp: element.parentElement,
      ArrowDown: element.firstElementChild,
      ArrowLeft: element.previousElementSibling,
      ArrowRight: element.nextElementSibling,
    }[event.key]
    if (next?.hasAttribute('data-scout-node')) {
      event.preventDefault()
      focus(next)
    }
  }
  let hovered = ''
  const hover = (event) => {
    if (!props.prototype) return
    const id = hitNode(event)?.getAttribute('data-scout-node') || ''
    if (id !== hovered) {
      hovered = id
      emit('candidate', id)
    }
  }
  const leave = () => {
    hovered = ''
    emit('candidate', '')
  }
  document.addEventListener('click', click, true)
  document.addEventListener('keydown', key)
  document.addEventListener('scroll', update, true)
  document.addEventListener('pointermove', hover)
  document.addEventListener('focusin', hover)
  document.addEventListener('pointerleave', leave)
  frame.value.contentWindow.addEventListener('resize', update)
  cleanup = () => {
    document.removeEventListener('click', click, true)
    document.removeEventListener('keydown', key)
    document.removeEventListener('scroll', update, true)
    document.removeEventListener('pointermove', hover)
    document.removeEventListener('focusin', hover)
    document.removeEventListener('pointerleave', leave)
    document.defaultView?.removeEventListener('resize', update)
  }
  update()
}
watch(
  () => [props.nodeIds, props.cardIds, props.selectionId, props.hoverId],
  () => nextTick(update),
  { deep: true },
)
watch(
  () => props.selectionId,
  async (id) => {
    if (!props.prototype || !/^n\d+$/.test(id || '')) return
    await nextTick()
    frame.value?.contentDocument
      ?.querySelector(`[data-scout-node="${id}"]`)
      ?.scrollIntoView({ block: 'center', inline: 'nearest' })
    update()
  },
)
onMounted(() => {
  observer = new ResizeObserver(update)
  if (frame.value) observer.observe(frame.value)
})
onUnmounted(() => {
  cleanup()
  observer?.disconnect()
  themeObserver?.disconnect()
  cancelAnimationFrame(animation)
})
</script>

<template>
  <div class="scout-snapshot-frame">
    <iframe
      ref="frame"
      :srcdoc="html"
      sandbox="allow-same-origin"
      referrerpolicy="no-referrer"
      :title="t('scout.visual.snapshotTitle')"
      @load="attach"
    />
    <svg class="scout-snapshot-highlights" aria-hidden="true">
      <rect
        v-for="box in rectangles"
        :key="`${box.role}-${box.id}`"
        :data-role="prototype ? box.role : undefined"
        :x="box.x"
        :y="box.y"
        :width="box.width"
        :height="box.height"
      />
    </svg>
  </div>
</template>
