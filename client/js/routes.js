import '../styles/scout-comparison.scss'
import '../styles/scout-retention.scss'
import '../styles/scout-workflow.scss'
import '../prototypes/inspector.scss'
import '../styles/scout-design.scss'

const loadHome = () => import('../components/ScoutPage.vue')

export default {
  ScoutComparison: {
    path: '/scout/compare',
    component: () => import('../components/ScoutComparisonPage.vue'),
    meta: { titleKey: 'scout.comparison.title', requiresAuth: true },
  },
  ScoutStorage: {
    path: '/scout/storage',
    component: () => import('../components/ScoutRetentionPage.vue'),
    meta: { titleKey: 'scout.retention.title', requiresAuth: true },
  },
  ScoutUX004: {
    path: '/scout/ux-004',
    component: () => import('../prototypes/ScoutUX004.vue'),
    meta: { titleKey: 'scout.routes.home', requiresAuth: true },
  },
  ScoutHome: {
    path: '/scout',
    component: loadHome,
    meta: {
      titleKey: 'scout.routes.home',
      requiresAuth: true,
    },
  },
}
