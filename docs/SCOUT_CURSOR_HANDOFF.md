# SCOUT: задание для Cursor и интеграция с ERGOMS

Дата: 9 октября 2026 года. Цель — реализовать систему сбора данных в репозитории https://github.com/ergoms/ergo_ms_scout с соблюдением контрактов ERGOMS.

Это план реализации, а не существующий код. Сопутствующий PARSER_ARCHITECTURE.md содержит подробные требования и открытые вопросы. Начать с вертикального сценария первой фазы, затем двигаться по этапам. Не выдавать заглушки или один интерфейс за готовый сбор данных.

## 1. Что изучено и где

Исследованы документы и ключевые исходники через GitHub. Полный стек не запускался, тесты ERGOMS не выполнялись. Точные inspected refs:

| Репозиторий | Ветка | Commit/tree SHA |
|---|---|---|
| ergo_ms_core | dev | 1e4ac51d48d1c47cf95abc7773639bbf7c94b93b |
| ergo_ms_core_api | dev | 3ad44fb609f17f39545ec707f42dd503622b73e8 |
| ergo_ms_core_client | dev | 10ffa5d78f1327a3d4f415343a92da40cd1ddb92 |
| module_template | dev | 8168a9de2d82f4d04827b1ea1d929fc75178382f |
| ergo_ms_ollama_framework | dev | 4ac3b83116cb0c446da09b0dd7f15c5d8355e112 |
| ergo_ms_scout | main | Пока пустой; GitHub tree API вернул 409 |

Пути ниже заданы относительно соответствующего репозитория. В рабочем дереве платформы API находится в core/api, клиент — core/client, модули — modules/<name>.

| Механизм | Проверенный источник | Что брать для SCOUT |
|---|---|---|
| Общая архитектура | [core/.docs/architecture.md](https://github.com/ergoms/ergo_ms_core/blob/dev/.docs/architecture.md) | Django, Vue, модули, файлы и очередь |
| Вынос процесса | [core/.docs/modularization.md](https://github.com/ergoms/ergo_ms_core/blob/dev/.docs/modularization.md) | MODULE_RUNTIME, bridge, API/worker/beat, схемы |
| Hook-каталог | [core/.docs/modules.md](https://github.com/ergoms/ergo_ms_core/blob/dev/.docs/modules.md) | Все точки расширения |
| Обнаружение API | [core_api/src/core/utils/auto_api/auto_config.py](https://github.com/ergoms/ergo_ms_core_api/blob/dev/src/core/utils/auto_api/auto_config.py) | ModuleDiscoverer, обход apps.py |
| Зависимости | [core_api/src/core/utils/auto_api/module_integrations.py](https://github.com/ergoms/ergo_ms_core_api/blob/dev/src/core/utils/auto_api/module_integrations.py) | requires/extends |
| Мост | [core_api/src/core/integrations/bridge.py](https://github.com/ergoms/ergo_ms_core_api/blob/dev/src/core/integrations/bridge.py) | provide_op, call, provide_many, all, emit |
| HTTP-мост | [core_api/src/core/integrations/transports/http.py](https://github.com/ergoms/ergo_ms_core_api/blob/dev/src/core/integrations/transports/http.py) | Межпроцессные JSON-вызовы |
| События | [core_api/src/core/integrations/transports/redis_bus.py](https://github.com/ergoms/ergo_ms_core_api/blob/dev/src/core/integrations/transports/redis_bus.py) | Pub/Sub для интеграции, не очередь работ |
| Очереди модуля | [core_api/src/core/utils/celery/module_queues.py](https://github.com/ergoms/ergo_ms_core_api/blob/dev/src/core/utils/celery/module_queues.py) | --module выбирает объявленные очереди |
| Worker | [core_api/scripts/start_celery_worker.py](https://github.com/ergoms/ergo_ms_core_api/blob/dev/scripts/start_celery_worker.py) | --queues, --hostname, --concurrency, --pool |
| Схема БД | [core_api/src/core/utils/database/module_schema.py](https://github.com/ergoms/ergo_ms_core_api/blob/dev/src/core/utils/database/module_schema.py) | api/schema.yaml, m_scout, isolated |
| Клиентский контракт | [core_client/src/modules/core/clientModuleManifest.js](https://github.com/ergoms/ergo_ms_core_client/blob/dev/src/modules/core/clientModuleManifest.js) | moduleKey, routes, endpoints, guards, locales |
| Живой шаблон | [module_template/AGENTS.md](https://github.com/ergoms/module_template/blob/dev/AGENTS.md) | Компоненты UI, public_id, media_api, правила |
| Фоновые задачи | [module_template/api/celery_config.py](https://github.com/ergoms/module_template/blob/dev/api/celery_config.py) | CeleryModuleConfig и task routes |
| Меню | [module_template/api/migrations/0004_add_menu.py](https://github.com/ergoms/module_template/blob/dev/api/migrations/0004_add_menu.py) | MenuMigrationHelper, новая миграция |
| LLM | [ollama_framework/api/integrations.py](https://github.com/ergoms/ergo_ms_ollama_framework/blob/dev/api/integrations.py) | ollama_framework.chat/generate через bridge |

### Важное расхождение версий

Мета-репозиторий закрепляет core/api на e7869558b35414c643551f067b9606deca88c09d, core/client на 9e95494a79066e0f7e1c794325a87a841d12a443, module_template на f7a2edb8f86e0cdfd9d01d1d6c480314fc78319a. Это другие SHA, чем вершины dev.

Для bridge.py, start_celery_worker.py и clientModuleManifest.js проверенные blobs на gitlink совпали с dev. Но api/schema.yaml в закреплённом module_template отсутствует, а в текущем dev есть. Перед копированием проверить фактический checkout. Не обновлять все submodules автоматически и не копировать неизвестные файлы на основании документации.

## 2. Что должен делать SCOUT

Подтверждённые пользователем возможности:

- Визуальная/ручная настройка задания и настройка с помощью LLM.
- Вход по URL либо поиск сайтов по теме, например «Станки».
- HTML и сайты сначала; PDF/DOCX и другие источники через будущие плагины.
- Исследование источников, предложение полей, схемы и границ обхода.
- Предпросмотр и обязательное подтверждение перед основным сбором.
- Выбор DOM-элементов кликами по скачанной странице.
- Редактор из блоков со связями и параметрами при нажатии на блок.
- Краулер обнаруживает URL, загрузчик сохраняет страницы, парсеры независимо извлекают записи.
- Несколько исполнителей внутри запуска; поддержка одного исполнителя каждого типа.
- Веб-интерфейс и CLI.
- Пауза, отмена, восстановление, расписание и сравнение повторных запусков.
- Происхождение полей, проверка, дубликаты и ручной разбор спорных записей.
- JSON/CSV/XLSX, БД и файлы; отдельное публичное API продукта не требуется.
- Плагины и исполнители на разных языках.
- Первая версия на одной машине; возможность вынести процессы на другие машины позже.

Служебное Django API для интерфейса, CLI и процессов требуется, несмотря на отсутствие потребности в публичном API выдачи результатов.

Подтверждено 10 октября 2026 года: первая версия работает на Windows и на Linux; LLM в ней нет, позже модель только предлагает схему, правила и план; клик по полю карточки предлагает такие же поля на проверку; заданий в списке много, одновременно выполняется один сбор; «Станки» — список сайтов и страниц по теме.

Не считать выбранными: основной язык ядра как отдельное решение пользователя, конкретные поисковые провайдеры и допустимый объём. Рабочее допущение: ядро модуля на Python, другие языки — исполнители по JSON-контракту. Обычный сбор по URL этими открытыми пунктами не блокируется.

## 3. Границы реализации

Репозиторий ergo_ms_scout — один модуль платформы. Предложенное имя папки: modules/scout; Django label: scout; API prefix: /api/scout/; клиентский namespace: scout. Веб-название — SCOUT.

Внутри модуля отдельные роли: coordinator, search, discovery, fetch_http, fetch_browser, crawl, extract, quality, export. Каждую роль можно запускать своим процессом. Не создавать отдельную модель Django, репозиторий или микросервис для каждой таблицы только ради числа сущностей.

Ядро предоставляет пользователей, JWT, права, меню, аудит, media_api, realtime, lifecycle и Celery. SCOUT владеет заданиями, обходом, схемами, результатами, плагинами и процессом подтверждения.

Не добавлять предметную логику SCOUT в core. Межмодульные операции — через ModuleBridge. Внутри SCOUT разрешены обычные относительные импорты. Новые доменные контракты SCOUT хранятся в модуле; константы платформы импортируются из ядра.

## 4. Предложенная структура репозитория

Создавать файлы по мере этапов, без пустых файлов ради дерева.

| Путь относительно SCOUT | Назначение |
|---|---|
| api/apps.py | AppConfig modules.scout.api / label scout, ready → integrations |
| api/models/ | Job, Run, Task, Snapshot, Record, Evidence и версии |
| api/services/coordinator/ | План, переходы, подтверждение, выдача и восстановление |
| api/services/discovery/ | Исследование сайта, предложения схемы |
| api/services/crawl/ | Frontier, фильтры URL, пагинация, лимиты |
| api/services/extraction/ | Правила, предпросмотр, DOM-локаторы |
| api/services/quality/ | Нормализация, проверка, идентичность |
| api/plugins/ | Реестр и адаптеры расширений |
| api/contracts/ | Версионированные описания операций и артефактов |
| api/tasks.py | Тонкие Celery-задачи, вызывающие доменные сервисы |
| api/celery_config.py | Очереди и маршруты scout.* |
| api/celery_beat_config.py | Расписание и reconciler, после проверки API платформы |
| api/integrations.py | Ops/groups, аудит, media quota, удаление пользователя |
| api/bridge_manifest.yaml | Декларация публичных bridge ops/groups |
| api/schema.yaml | schema: m_scout; isolated: true |
| api/serializers/, api/views/, api/urls.py | API редактора и управления запусками |
| api/permission_catalog.py, api/permissions.py | Права view/configure/run/review/export |
| api/migrations/ | Модели, новые меню и права |
| api/user_guides/ | Пользовательская справка для ядра/RAG |
| client/js/ | routes, endpoints, permission-rules, locales, composables |
| client/components/ | Задания, граф, DOM-выбор, запуск, результат, проверка |
| client/federation-entry.js | Только если нужен ручной remote; генерация платформы тоже допустима |
| scripts/ | CLI, команды зарегистрированы через ergoms.conf |
| plugins/ | Примеры расширений: manifest и код собственного исполнителя |
| tests/fixtures/ | Статические и динамические HTML для воспроизводимых проверок |
| task_footprint.yaml | Ресурсы задач; исходные оценки явно помечены |
| host_lifecycle.yaml, process_roles.yaml | Роли и службы без дублирования ядра |
| ergoms.conf, ergoms.help.yaml, vscode.tasks.yaml | Команды и задачи редактора |
| pyproject.toml, client/package.json | Только собственные зависимости |
| README.md, AGENTS.md, HOWTO.md | Интеграция, команды, ограничения |

## 5. Конкретные правила платформы

1. Скопировать только нужные паттерны текущего module_template; переименовать module_template во всех namespace, labels, маршрутах, разрешениях и bridge ops. Не переносить учебную ML-логику и CRUD TemplateItem.
2. Использовать PublicIdMixin; снаружи только public_id. UUID пользователя хранить как user_public_id, без FK на auth_user. Между собственными сущностями SCOUT FK допустимы.
3. Views и проверки прав — через существующие базовые классы платформы; exact import взять из актуального api/views.py шаблона, не угадывать расположение.
4. Меню регистрировать собственной миграцией MenuMigrationHelper; не копировать историю шаблонных миграций и их устаревшие dependency номера.
5. Пользовательские файлы и выгрузки выдавать через media_api с signed URL. Временные снимки — через project_layout/cache_dir, не в папке исходников и не в /tmp как единственное хранилище.
6. UI использует существующие FormCard/FormField, DataTable, SelectBox, ModalCenter, LoadingContentArea, toast и useAppI18n, где они подходят. Не создавать вторую оболочку приложения.
7. Запуск, миграции и зависимости — через ergoms. Не придумывать новые команды платформы; отсутствующие команды SCOUT объявлять в ergoms.conf и help.
8. Долгие операции не выполнять внутри request/response, синхронного bridge.call или shell Cursor. Вызов управления возвращает идентификатор операции/запуска; работа — Celery или управляемый исполнитель.
9. ollama_framework сделать опциональным. Отсутствие модуля/модели показывает недоступность LLM-режима, а обычный сбор продолжает работать. Не импортировать modules.ollama_framework напрямую.
10. Клиентский bundled hooks и remote/standalone сохраняют общий контракт. Если написан federation-entry.js, проверить все страницы, а не только два шаблонных lazy imports: в текущем образце PatternsPage не сопоставлен в resolveComponent.

## 6. Координатор, данные и надёжность

### Сущности

Минимум для первого вертикального сценария: Job, JobRevision, Run, Task, Attempt, PageSnapshot, ExtractionSchemaRevision, ExtractionRulesRevision, Record, Evidence, ExportArtifact. Позже добавить SearchSpec/SearchResult/SourceCandidate, PluginVersion и WorkerRegistration.

Job — редактируемая конфигурация, Run — запуск закреплённой конфигурации. Task описывает единицу работы, Attempt — попытку. Snapshot неизменяемый артефакт с исходным/конечным URL, статусом, временем, хешем и ссылкой на содержимое. Запись может появляться несколько раз в разных запусках: отделять идентичность сущности от версии результата.

### Подтверждение

Развести ограниченное исследование и основной обход. Во время предварительного исследования действуют фиксированные лимиты страниц, времени и затрат. Основной сбор разрешён только при approval, привязанном к хешу/версии плана. Изменение источников, схемы или границ создаёт новую revision и снимает старое подтверждение.

### Состояния

Run: draft → exploring → awaiting_confirmation → running → completed / completed_with_errors / failed. Управляющие переходы: running → pausing → paused → running; running/paused → cancelling → cancelled. Действительные переходы проверяет сервер, а не только UI. Task: pending/leased/running/succeeded/retry_wait/failed/cancelled. Ручная проверка данных имеет отдельные статусы и не должна терять завершённые технические задачи.

### Выдача и восстановление

- БД SCOUT — источник истины; сообщения Celery содержат Task public_id и минимум метаданных.
- Создание Task и намерения выдать работу фиксируется атомарно; dispatcher/reconciler повторно публикует pending/просроченные операции.
- Worker захватывает Task по lease, фиксирует attempt и fencing token; поздний ответ старого исполнителя не перезаписывает результат нового.
- Повторная доставка безопасна: уникальные ключи результата по snapshot + версиям правил/схемы; транзакции переходов и записи результатов.
- Не полагаться только на transaction.on_commit для выдачи задачи: процесс может упасть между commit и отправкой. Нужен сохраняемый pending state с повторной выдачей.
- Pause прекращает выдачу новой работы, активные операции завершаются до безопасной границы. Cancel запрещает планирование следующих этапов и управляет текущими операциями без разрушения уже сохранённых результатов.
- Обход завершён только когда frontier пуст, активных операций обнаружения нет, повторы решены и downstream завершён. Пустая брокерная очередь сама по себе не доказывает завершение.
- Одновременные лимиты источника общие для процессов, а не отдельный semaphore в памяти каждого загрузчика. URL-нормализация не должна удалять смысловые query-параметры.

### Ограничение существующего EventBus

RedisEventBus._publish использует client.publish. При отсутствии клиента возвращает, а обычные ошибки публикации логирует. emit также перехватывает ошибки обработчиков. Поэтому событие может не быть доставлено, хотя вызов вернулся успешно.

В core_api/src/core/integrations/outbox.py publish_pending_outbox вызывает bridge.emit и помечает опубликованным после возврата. Для SCOUT это не гарантия исполнения или долговечной доставки. Использовать БД + Celery + повторную сверку; bridge-события — сигнал об изменении состояния, с возможностью перечитать состояние по API. Не менять ядро только ради задач SCOUT в первой фазе.

## 7. Процессы и очереди

Предложенные очереди: scout, scout_search, scout_fetch, scout_browser, scout_crawl, scout_extract, scout_quality, scout_export. На первой фазе создавать только нужные. Celery task names должны начинаться с modules.scout., маршруты — явными очередями.

Worker --module=scout по умолчанию слушает все очереди модуля. Для выделения этапов есть --queues. Ниже пример после создания этих очередей и настройки broker; команды пока НЕ выполнялись:

```bash
ergoms start-worker --module=scout --queues=scout_fetch --hostname=scout-fetch@local --concurrency=4 --pool=threads
ergoms start-worker --module=scout --queues=scout_extract --hostname=scout-extract@local --concurrency=2 --pool=threads
```

Для CPU-bound обработки отдельно проверить допустимый pool целевой ОС. Число Celery слотов не равняется лимиту HTTP-запросов на домен. Для автоматического запуска нескольких воркеров использовать существующие celery_workers.yaml/ergoms механизмы; отдельные служебные роли уточнить после первого рабочего сценария.

После первой фазы проверить start-module --module=scout, MODULE_RUNTIME=microservice, BRIDGE_TRANSPORT=http, bridge_manifest/schema и user_public_id. Для удалённых исполнителей потребуются транспорт, доступ к артефактам и идентичность сервисов — один переключатель MODULE_RUNTIME этого не решает.

## 8. Поиск, схема и визуальный выбор

Поиск — отдельный блок перед сбором, а не замена краулеру. Поисковые плагины возвращают URL/title/snippet/query/provider/rank/time. Сохранять provenance нескольких движков. Параметры: язык/регион, результат на запрос, число запросов, время и бюджет. Запрос «Станки» не определяет целевые поля: показать варианты цели либо запросить уточнение.

DuckDuckGo Instant Answer API не является полной выдачей сайтов; не выдавать его за готовый search provider. Выбор способа подключения DuckDuckGo и другого движка — отдельное решение. Для тестов использовать фикстуры ответа провайдера; реальный provider подключить до утверждения поискового сценария готовым.

Определение схемы: источник → небольшая выборка → предложения/клики → preview → подтверждение. Схема и DOM-правила — разные версионированные сущности. Непредсказуемые характеристики хранить в attributes, а raw и normalized value — раздельно.

Для кликов нужен DOM-снимок, не один PNG. Браузер может предварительно исполнить JS для получения итогового DOM. В селекторе показать изолированную копию без произвольных скриптов и привилегий основного приложения. Настоящие ссылки/формы не должны уводить пользователя или отправлять данные при выборе элемента. Сохранить mapping элементов preview к исходному DOM, включая сохранённые локальные ресурсы.

Клик создаёт кандидаты локаторов, а preview показывает совпадения. Один элемент, повторяющиеся карточки и пагинация требуют разных правил. Проверять найденное правило на других страницах. Для повторяющихся карточек подтверждено: система предлагает такие же поля и показывает совпадения на проверку. В правила они попадают после подтверждения пользователя. Пагинация задаётся отдельно.

## 9. Пайплайн и плагины

PipelineSpec: version, input, nodes, edges, source_scope, limits, output. Узел: id, type, plugin id/version, config, входы/выходы. Координатор валидирует граф, доступность операций, типы артефактов и параметры.

Для первой версии граф этапов ацикличен, повторяющийся обход находится внутри crawl/frontier с условиями завершения. Это позволяет избежать произвольных циклов графа без отрицания того, что сам краулинг цикличен. Возврат на ручную проверку — управляемое состояние, а не бесконечная петля редактора.

Типы расширений: search_provider, source, fetcher, extractor, normalizer, validator, exporter, document_processor, llm_provider. Плагин объявляет config/input/output schemas, version, protocol_version, capabilities, ресурсные требования и runner.

Реализовать Python-adapter сначала, но заранее определить нейтральный JSON-контракт. Затем доказать мультиязычность маленьким Node.js либо иным исполнителем одной операции. Произвольный язык не становится Celery worker автоматически; нужен адаптер subprocess или HTTP.

Предложенный envelope: protocol_version, operation, run_id, task_id, attempt_id, plugin_id, plugin_version, config, input_artifacts. Ответ: status, output_artifacts, result, metrics, error с retryable. Контроль: timeout, cancel, heartbeat и fencing token. Межмашинный режим не передаёт локальные абсолютные пути как общедоступные артефакты.

Установка плагинов с перезапуском конкретного исполнителя; версии закреплены на Run. Не требовать hot reload в первой версии. Сторонний код не загружать в координатор. Контракты и adapter checks проверять независимо от языка.

## 10. Этапы реализации и критерии готовности

### Фаза 0 — каркас платформы

Прочитать фактические AGENTS.md и .cursor/rules; установить SCOUT как modules/scout. Создать AppConfig, health, права, меню и страницу на компонентах ядра. Проверить discovery API/client и изоляцию. Не переписывать авторизацию, меню, media_api или lifecycle.

SCOUT пустой: не выполнять submodule add для empty repository как будто commit уже существует. Сначала создать локальный начальный каркас/commit в SCOUT. Публикацию commit и добавление gitlink в мета-репозиторий согласовывать как отдельные изменения рабочего дерева; не пытаться лечить отсутствие commit сбросом существующих submodules. При разработке в локальной обычной папке модуль может обнаруживаться до появления gitlink; финальное подключение оформить по правилам проекта.

### Фаза 1 — рабочий сценарий URL → данные → файл

Добавить модели и версии, ограниченную предварительную загрузку, ручные правила CSS/XPath, preview и обязательное подтверждение. Выполнить загрузку/извлечение через Celery. Записать HTML, строки результата, Evidence. Экспорт JSON/CSV через media_api.

Готовность: реальная HTML-фикстура или небольшой согласованный сайт превращается в записи; повторный парсинг использует сохранённый снимок; запуск без approval отклоняется сервером; повтор задачи не дублирует записи; остановленный worker не уничтожает Run.

### Фаза 2 — краулинг и исполнители

Frontier, фильтры домена/URL, глубина, max_pages/max_time, пагинация, несколько fetch/extract workers, pause/resume/cancel, reconciler и общие лимиты.

Готовность: два worker не обрабатывают одну Task конкурентно; очередь завершения корректна; пропавший worker восстанавливается; остановка координатора не теряет результаты; возобновление не перекачивает без необходимости готовые снимки.

### Фаза 3 — визуальная настройка

DOM-выбор и граф с панелью параметров. UI и CLI сохраняют одинаковые JobRevision/PipelineSpec. Валидировать связи, типы и недоступные плагины. Добавить браузерный fetch для JS-страниц.

Готовность: клики дают проверяемое правило, preview совпадает с извлечением; ссылки/формы в preview не исполняют нежелательные действия; сохранённый граф воспроизводит запуск после перезагрузки интерфейса.

### Фаза 4 — поиск и расширения

Реальный search provider, объединение результатов, подтверждение источников. Ввести plugin contract и один внешний исполнитель другого языка. Добавить XLSX, расписание и сравнение Run по доступности данных.

Готовность: query-only ввод даёт настоящие кандидаты источников; лимиты поиска соблюдены; один недоступный provider не обнуляет другие; плагин заменяется без изменения coordinator; новое расписание не требует правки ядра.

### Фаза 5 — LLM и межпроцессная проверка

Опциональный bridge-adapter ollama_framework для настройки по естественному языку. Модель предлагает строго проверяемый план, сохраняет версию и стоимость/метрики; approval всё равно обязателен. LLM-extraction включать только после отдельного уточнения.

Проверить monolith и отдельный API SCOUT в microservice-режиме на одной машине, отказ модуля без падения оболочки. Последующее разнесение машин — отдельная фаза с доступом к артефактам, защищённым транспортом и требованиями восстановления.

## 11. Необходимые проверки

- Discovery, миграции, права на свои/чужие Run и артефакты.
- Запуск без approval; изменение плана после approval.
- Повторная доставка, падение до/после сохранения результата, fencing старой попытки.
- Пауза/возобновление, отмена, исчерпание лимитов, циклические ссылки.
- Блокировка/429/таймаут, ошибка parser/plugin, частичные результаты.
- Preview и настоящее извлечение по одному Snapshot; происхождение каждого значения.
- Недоступность Ollama без разрушения ручного режима.
- Подмена DOM в preview не влияет на основную авторизованную оболочку.
- Плагин с несовместимой версией контракта; внешний executor другого языка.
- Monolith и split process; общие доменные лимиты между worker.

Где возможно, сетевые сценарии проверять на локальном HTTP-стенде, а не зависеть от постоянно меняющейся выдачи. Проверка реального search adapter выполняется отдельно. Не обещать универсальность на основании одного сайта.

## 12. Ожидаемый отчёт Cursor

Создать docs/IMPLEMENTATION_STATUS.md: что реализовано, какие файлы изменены, команды через ergoms, результаты проверок, рабочий пример Run, ограничения и следующий этап. Отличать работающий код, mock adapter и будущие возможности. Называть службы, которые должны оставаться запущенными для долгих заданий.

Начать с фазы 0 и фазы 1, выполнить проверки этой фазы. Остальные этапы сохранить в плане. Не начинать с большой визуальной демонстрации без реального сохранения, извлечения и восстановления.

## 13. Короткий стартовый запрос для Cursor

Прочитай SCOUT_CURSOR_HANDOFF.md и PARSER_ARCHITECTURE.md. Реализуй фазы 0–1 как настоящий модуль ERGOMS в modules/scout из репозитория ergo_ms_scout. Сначала проверь checkout ядра/шаблона, AGENTS.md и правила. Используй discovery, ModuleBridge, Celery, права, меню и media_api платформы. Сохрани будущие роли как отдельные контракты. Дай рабочий сценарий URL → preview → подтверждение → Celery → данные с Evidence → JSON/CSV, с повторным парсингом снимка и безопасным повтором Task. Не добавляй отдельный broker или новую оболочку. Зафиксируй команды, проверки и оставшиеся этапы в docs/IMPLEMENTATION_STATUS.md.

## 14. Повторное использование существующих парсеров

Дополнение от 10 октября 2026 года: пользователь передал dev ZIP обоих модулей; исходники изучены. Предыдущая невозможность чтения через GitHub 404 больше не блокирует анализ этих архивов. Полный разбор и точные SHA-256 архивов — в SCOUT_PARSER_REUSE_REVIEW.md. Commit SHA архивов неизвестен; перед переносом сверить checkout.

### Что использовать

- vacancies_parser: hh_selenium/hh_pages.py и hh_fields.py как предметный HTML-extractor; паттерны браузерной загрузки, отдельных очередей, нормализации и RecordRevision.
- education_materials_parser: university_catalog/extract.py для предметного обнаружения программ/ссылок, политику download_retry, extract_provenance и extract_metrics.
- PDF/DOCX/XLSX/OCR, таблицы, шаблонные схемы и LLM на пробелы — будущие плагины, не обязательные зависимости HTML-MVP.
- Готовые данные старых модулей читать через ModuleBridge и optional source adapters. Не импортировать их модели. Существующие read ops не являются готовым протоколом управления Run.

### Ограничения, которые Cursor обязан учитывать

1. vacancies_parser.iter_vacancies(source='all') теряет записи при общей пагинации: воспроизведение на двух источниках пропустило все четыре записи второго источника. Для полного импорта перечислять конкретные source отдельно либо исправить merge у владельца.
2. VacancySnapshot хранит извлечённые поля, description_raw — только HTML описания. Для независимых парсеров SCOUT сохранять полную страницу как PageSnapshot.
3. В upsert_vacancy/create_vacancy_once карточка и snapshot записываются раздельно. Новую версию и результат Task SCOUT записывать атомарно с контролем конкуренции.
4. Файловые замки, .queued, общая JSON-статистика и run stamp старых модулей не переносить как основу независимых многомашинных Run. Состояние, lease, dispatch_pending и лимиты — в БД SCOUT.
5. Хеш текста старого extraction не включает версию схемы/правил. SCOUT закрепляет snapshot hash + schema/rules/plugin revision и необходимые параметры модели.
6. Старый provenance расширить до Evidence конкретного значения и исходного снимка; не выдумывать DOM-локатор для импортированной записи без полного HTML.
7. university_catalog.documents.list не имеет курсора/offset и ограничен 200 записями. Его get не отдаёт полный DTO источника/файла: для полного импорта расширить контракт через владельца.
8. Схемы ФГОС/РПД и прочие предметные поля заранее написаны разработчиком. Это не автоматическое определение схемы нового сайта.
9. Не переносить фильтр только IT, учебные коды, расписания площадки, KAG/GPU и правила перевода в универсальный координатор.
10. integrations.yaml education в архиве использует extends для ai_assistant/ollama_framework/organizations; строка requires в README устарела.

### Проверка и следующий шаг

Синтаксис всех 571 Python-файла прошёл ast.parse. Из 20 выбранных тестовых методов качества/таблиц в изолированном harness 17 прошли, три заблокированы отсутствием Django. В harness SimpleTestCase заменён в памяти на unittest.TestCase; это не штатный запуск тестов ERGOMS и не проверка БД/браузера/LLM.

Фазы 0–1 остаются первыми. Начать с общего HTML-снимка, ручных CSS/XPath-правил, preview/approval, независимого извлечения, Evidence и JSON/CSV. Первым предметным примером позже может стать HH extractor. Не начинать с копирования двух модулей целиком. Зафиксировать происхождение перенесённых компонентов в docs/REUSE_MAP.md.

