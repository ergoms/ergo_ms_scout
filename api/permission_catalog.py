from .permissions import (
    MODULE_NAME,
    SCOUT_CONFIGURE,
    SCOUT_EXPORT,
    SCOUT_REVIEW,
    SCOUT_RUN,
    SCOUT_VIEW,
)

PERMISSION_CATALOG = {
    'module_name': MODULE_NAME,
    'user_description': (
        'Сбор данных с HTML-страниц: адрес, правила, подтверждение и выгрузка файла.'
    ),
    'permissions': {
        SCOUT_VIEW: 'Просмотр заданий и результатов',
        SCOUT_CONFIGURE: 'Настройка задания и правил',
        SCOUT_RUN: 'Запуск, пауза и отмена сбора',
        SCOUT_REVIEW: 'Подтверждение плана перед сбором',
        SCOUT_EXPORT: 'Выгрузка JSON и CSV',
    },
}
