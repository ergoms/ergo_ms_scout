"""Константы координатора. Будущие очереди не создаются в первой фазе."""

EXTRACTOR_ID = 'scout.extractor.html'
EXTRACTOR_VERSION = '2'
PROTOCOL_VERSION = 1

TASK_FETCH = 'fetch'
TASK_CRAWL = 'crawl'
TASK_EXTRACT = 'extract'

CRAWL_QUEUED = 'queued'
CRAWL_ACTIVE = 'active'
CRAWL_DONE = 'done'
CRAWL_SKIPPED = 'skipped'
CRAWL_FAILED = 'failed'

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

CRAWL_LIMITS = {
    'max_depth': 2,
    'max_pages': 50,
    'preview_pages': 3,
    'max_seconds': 600,
    'frontier_cap': 200,
    'fetch_delay_ms': 0,
    'parallel_fetch': 2,
    'max_total_bytes': 20_000_000,
}

DROP_QUERY = (
    'utm_source',
    'utm_medium',
    'utm_campaign',
    'utm_term',
    'utm_content',
    'gclid',
    'fbclid',
)

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
