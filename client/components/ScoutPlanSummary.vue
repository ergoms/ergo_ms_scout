<script setup>
import { computed } from 'vue'
import { useAppI18n } from '@/i18n/useAppI18n.js'
import DataTable from '@/components/DataTable.vue'
import ScoutCrawlSummary from './ScoutCrawlSummary.vue'

const props = defineProps({
  plan: { type: Object, required: true },
  mode: { type: String, default: 'manual' },
  expanded: { type: Boolean, default: false },
  approved: { type: Boolean, default: false },
})
const { t } = useAppI18n()
const stateLabel = computed(() =>
  t(props.approved ? 'scout.workspace.planApproved' : 'scout.workspace.planWaiting'),
)
const bounds = computed(() =>
  props.plan.crawl?.mode === 'site'
    ? t('scout.workspace.siteBounds', {
        depth: props.plan.crawl.max_depth,
        pages: props.plan.crawl.max_pages,
      })
    : t('scout.flow.pagesOnly'),
)
const fieldColumns = computed(() => [
  { key: 'name', label: t('scout.fieldName') },
  { key: 'format', label: t('scout.product.conversion') },
  { key: 'selector', label: t('scout.fieldKind') },
  { key: 'scope', label: t('scout.fieldScope') },
])
</script>

<template>
  <div v-if="expanded" class="scout-plan--expanded scout-confirmation">
    <section class="scout-confirmation__section">
      <h3>{{ t('scout.design.sourceProcessing') }}</h3>
      <dl class="scout-confirmation__facts">
        <div class="scout-confirmation__wide">
          <dt>{{ t('scout.url') }}</dt>
          <dd>
            <ul v-if="plan.seed_urls?.some(Boolean)" class="list-unstyled mb-0">
              <li v-for="(url, index) in plan.seed_urls" :key="index">{{ url }}</li>
            </ul>
            <template v-else>{{ t('scout.workspace.notSpecified') }}</template>
          </dd>
        </div>
        <div>
          <dt>{{ t('scout.workspace.recordUnit') }}</dt>
          <dd>
            <code v-if="plan.record">{{ plan.record.kind }} · {{ plan.record.selector }}</code>
            <template v-else>{{ t('scout.flow.oneRecord') }}</template>
          </dd>
        </div>
        <div>
          <dt>{{ t('scout.workspace.modeLabel') }}</dt>
          <dd>{{ t(`scout.workspace.modes.${mode}.title`) }}</dd>
        </div>
        <div v-if="plan.identity">
          <dt>{{ t('scout.product.identity') }}</dt>
          <dd>
            {{ plan.identity.kind }} · {{ plan.identity.namespace }} ·
            <code>{{ plan.identity.field }}</code>
          </dd>
        </div>
        <div>
          <dt>{{ t('scout.workspace.collectionBounds') }}</dt>
          <dd>{{ bounds }}</dd>
        </div>
        <div class="scout-confirmation__wide" v-if="plan.crawl?.mode === 'site'">
          <dt class="visually-hidden">{{ t('scout.workspace.collectionBounds') }}</dt>
          <dd><ScoutCrawlSummary :run="{ crawl: plan.crawl }" plan-details /></dd>
        </div>
        <div
          v-if="plan.crawl?.sitemap_url || plan.crawl?.sitemap_from_robots"
          class="scout-confirmation__wide"
        >
          <dt>Sitemap / robots.txt</dt>
          <dd>{{ plan.crawl.sitemap_url || t('scout.product.robots') }}</dd>
        </div>
        <div v-if="plan.crawl?.page_types?.length" class="scout-confirmation__wide">
          <dt>{{ t('scout.product.pageTypes') }}</dt>
          <dd v-for="type in plan.crawl.page_types" :key="type.id" class="scout-confirmation__type">
            <strong>{{ type.id }}</strong> · {{ t(`scout.product.${type.action}`) }}
            <code>{{ JSON.stringify(type.match) }}</code>
            <code v-if="type.selectors">{{ JSON.stringify(type.selectors) }}</code>
          </dd>
        </div>
      </dl>
    </section>
    <section class="scout-confirmation__section">
      <h3>{{ t('scout.design.planVersion') }}</h3>
      <p class="scout-confirmation__state">
        <span class="scout-badge" :class="approved ? 'scout-badge--saved' : 'scout-badge--failed'">
          <template v-if="plan.revision_number != null">№ {{ plan.revision_number }} · </template
          >{{ stateLabel }}
        </span>
      </p>
      <dl class="scout-confirmation__facts scout-confirmation__facts--stacked">
        <div v-if="plan.limits">
          <dt>{{ t('scout.workspace.requestLimits') }}</dt>
          <dd>
            {{
              t('scout.workspace.requestBudget', {
                bytes: plan.limits.max_bytes,
                seconds: plan.limits.timeout_seconds,
              })
            }}
          </dd>
        </div>
        <div>
          <dt>{{ t('scout.product.proxy') }}</dt>
          <dd>{{ plan.proxy_profile_revision_public_id || t('scout.product.direct') }}</dd>
        </div>
      </dl>
    </section>
    <section class="scout-confirmation__section scout-confirmation__fields">
      <h3>{{ t('scout.workspace.resultFields') }}</h3>
      <DataTable
        :items="plan.fields || []"
        :columns="fieldColumns"
        :get-item-key="(field, index) => field.name || index"
        :show-number-column="false"
        :enable-pagination="false"
        cards-below="sm"
      >
        <template #cell-name="{ item }">{{
          item.name || t('scout.workspace.notSpecified')
        }}</template>
        <template #cell-format="{ item }">
          {{
            item.value_source === 'image'
              ? t('scout.visual.image.field')
              : t(`scout.product.${item.conversion?.type || 'text'}`)
          }}
          <template v-if="item.conversion?.currency"> · {{ item.conversion.currency }}</template>
          <template v-if="item.conversion?.format"> · {{ item.conversion.format }}</template>
          <small v-if="item.conversion?.decimal_separator" class="d-block scout-muted"
            >{{ t('scout.product.decimal') }} · {{ item.conversion.decimal_separator }}</small
          >
        </template>
        <template #cell-selector="{ item }">
          <span>{{ item.kind?.toUpperCase() }} · </span
          ><code>{{ item.selector || t('scout.workspace.notSpecified') }}</code>
        </template>
        <template #cell-scope="{ item }">
          {{ item.scope === 'record' ? t('scout.scopeRecord') : t('scout.scopePage') }}
          <small v-if="item.many" class="d-block scout-muted">{{ t('scout.fieldMany') }}</small>
        </template>
      </DataTable>
    </section>
  </div>
  <dl v-else class="scout-plan">
    <div>
      <dt>{{ t('scout.workspace.modeLabel') }}</dt>
      <dd>{{ t(`scout.workspace.modes.${mode}.title`) }}</dd>
    </div>
    <div>
      <dt>{{ t('scout.url') }}</dt>
      <dd>
        <ul v-if="plan.seed_urls?.some(Boolean)" class="list-unstyled mb-0">
          <li v-for="(url, index) in plan.seed_urls" :key="index">{{ url }}</li>
        </ul>
        <template v-else>{{ t('scout.workspace.notSpecified') }}</template>
      </dd>
    </div>
    <div>
      <dt>{{ t('scout.workspace.resultFields') }}</dt>
      <dd>
        {{
          plan.fields
            ?.map((field) => field.name)
            .filter(Boolean)
            .join(', ') || t('scout.workspace.notSpecified')
        }}
      </dd>
    </div>
    <div>
      <dt>{{ t('scout.workspace.collectionBounds') }}</dt>
      <dd>{{ bounds }}</dd>
    </div>
    <div>
      <dt>{{ t('scout.workspace.planState') }}</dt>
      <dd>{{ stateLabel }}</dd>
    </div>
  </dl>
</template>
