const loadHome = () => import('../components/ScoutPage.vue')

export default {
  ScoutHome: {
    path: '/scout',
    component: loadHome,
    meta: {
      titleKey: 'scout.routes.home',
      requiresAuth: true,
    },
  },
}
