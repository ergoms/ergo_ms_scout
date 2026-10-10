export default [
  {
    match: (to) => to.path?.startsWith('/scout'),
    module: 'scout',
    permissions: ['scout_view'],
    title: 'Нет доступа к SCOUT',
    message: 'Нужно право просмотра заданий сбора.',
  },
]
