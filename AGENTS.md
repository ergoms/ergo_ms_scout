# SCOUT

Сбор записей с HTML-страницы внутри ERGO MS. Первый проход: адрес, правила, подтверждение, снимок, записи, JSON/CSV.

## Куда смотреть

- Как должен работать модуль — `BEHAVIOR.md`
- Что уже сделано и чего нет — `docs/IMPLEMENTATION_STATUS.md`
- План и подтверждение — `api/services/runs.py`
- Задачи — `api/services/tasks_exec.py`
- Страница — `client/components/ScoutPage.vue`

## Обязательно

- Основной сбор только после подтверждения хеша плана.
- Один сбор одновременно.
- Повтор задачи не создаёт вторую запись с тем же снимком и теми же правилами.
- Снаружи `public_id`, владелец `user_public_id`.

## Запрещено

- Импорт моделей другого модуля.
- Запуск многочасового обхода одной задачей Celery.
- Считать пустую очередь брокера доказательством, что сбор закончен.

## Команды

```text
ergoms start-worker --module=scout
ergoms scout:collect --url https://example.com --field title=css:h1 --user-public-id <uuid>
ergoms scout:approve-run --run <uuid> --user-public-id <uuid>
ergoms api test modules.scout.api
```

Для живого сбора нужны база, брокер и воркер `scout`. `ergoms stop` их останавливает, закрытие Cursor — нет.
