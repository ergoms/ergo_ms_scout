export default [
  {
    match: (to) => to.path?.startsWith('/scout'),
    module: 'scout',
    permissions: ['scout_view'],
    title: 'Нет доступа к Scout',
    message: 'Чтобы получить доступ к Scout, обратитесь к администратору ERGO MS.',
  },
]
