"""Константы координатора. Будущие очереди не создаются в первой фазе."""

EXTRACTOR_ID = 'scout.extractor.html'
EXTRACTOR_VERSION = '1'
PROTOCOL_VERSION = 1

TASK_FETCH = 'fetch'
TASK_EXTRACT = 'extract'

TASK_PENDING = 'pending'
TASK_LEASED = 'leased'
TASK_RUNNING = 'running'
TASK_SUCCEEDED = 'succeeded'
TASK_RETRY_WAIT = 'retry_wait'
TASK_FAILED = 'failed'
TASK_CANCELLED = 'cancelled'

RUN_EXPLORING = 'exploring'
RUN_AWAITING = 'awaiting_confirmation'
RUN_RUNNING = 'running'
RUN_PAUSING = 'pausing'
RUN_PAUSED = 'paused'
RUN_CANCELLING = 'cancelling'
RUN_CANCELLED = 'cancelled'
RUN_COMPLETED = 'completed'
RUN_COMPLETED_WITH_ERRORS = 'completed_with_errors'
RUN_FAILED = 'failed'

OCCUPYING_STATUSES = (
    RUN_EXPLORING,
    RUN_AWAITING,
    RUN_RUNNING,
    RUN_PAUSING,
    RUN_PAUSED,
    RUN_CANCELLING,
)

CLAIMABLE_RUN_STATUSES = (RUN_EXPLORING, RUN_RUNNING)
TERMINAL_RUN_STATUSES = (
    RUN_CANCELLED,
    RUN_COMPLETED,
    RUN_COMPLETED_WITH_ERRORS,
    RUN_FAILED,
)

OPEN_TASK_STATUSES = (TASK_PENDING, TASK_LEASED, TASK_RUNNING, TASK_RETRY_WAIT)

DEFAULT_LIMITS = {
    'max_pages': 5,
    'max_bytes': 1_000_000,
    'timeout_seconds': 20,
}

MAX_ATTEMPTS = 3
LEASE_SECONDS = 120
REPUBLISH_AFTER_SECONDS = 30
FIELD_TEXT_LIMIT = 8000

# Роли, которые позже получат свои очереди. Сейчас их нет в Celery.
FUTURE_QUEUES = (
    'scout_search',
    'scout_fetch',
    'scout_browser',
    'scout_crawl',
    'scout_extract',
    'scout_quality',
    'scout_export',
)
