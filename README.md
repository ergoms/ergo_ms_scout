# SCOUT

Модуль собирает записи с HTML-страницы. Пользователь задаёт адрес и правила CSS или XPath, смотрит предпросмотр и подтверждает план. Только после этого основной сбор пишет записи и даёт скачать JSON или CSV.

Поиск сайтов по теме, обход ссылок, клики по карточке и языковая модель в эту версию не входят. Одновременно выполняется один сбор. Первая версия рассчитана на Windows и Linux.

## Зависимости

Обязательных модулей нет. Очередь — Celery платформы, файлы выгрузки — media_api, пользователи и меню — ядро.

Для живого сбора должны быть запущены база, брокер очереди и воркер:

```text
ergoms start-worker --module=scout
```

Закрытие Cursor воркер не останавливает. `ergoms stop` останавливает службы проекта.

## Команды

```text
ergoms scout:collect --url https://example.com --field title=css:h1 --user-public-id <uuid>
ergoms scout:approve-run --run <uuid> --user-public-id <uuid>
ergoms api test modules.scout.api
```

Повторяющееся поле в команде отмечается звёздочкой: `title*=css:.card .title`.

Переменная `SCOUT_TASK_RUNNER=inline` в `modules/scout/.env.example` выполняет задачи в том же процессе. Для обычной работы оставьте `celery`.

## Куда смотреть

| Что | Где |
|---|---|
| План и подтверждение | `api/services/runs.py` |
| Загрузка и извлечение | `api/services/tasks_exec.py` |
| Страница | `client/components/ScoutPage.vue` |
| Как должен работать модуль | `BEHAVIOR.md` |
| Что уже сделано | `docs/IMPLEMENTATION_STATUS.md` |
