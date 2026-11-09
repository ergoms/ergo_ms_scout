"""Состояние сбора SCOUT. Снаружи только public_id. Владелец — user_public_id."""

from django.db import models

from src.core.utils.base.models import PublicIdMixin


class Job(PublicIdMixin, models.Model):
    user_public_id = models.UUIDField(db_index=True)
    title = models.CharField(max_length=200)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return self.title


class JobRevision(PublicIdMixin, models.Model):
    job = models.ForeignKey(Job, on_delete=models.CASCADE, related_name='revisions')
    revision_number = models.PositiveIntegerField()
    seed_urls = models.JSONField(default=list)
    fields = models.JSONField(default=list)
    record = models.JSONField(default=dict, blank=True)
    limits = models.JSONField(default=dict)
    crawl = models.JSONField(default=dict, blank=True)
    identity = models.JSONField(null=True, blank=True)
    proxy_profile_revision_public_id = models.UUIDField(null=True, blank=True)
    plan_hash = models.CharField(max_length=64)
    approved_plan_hash = models.CharField(max_length=64, blank=True, default='')
    approved_at = models.DateTimeField(null=True, blank=True)
    approved_by_public_id = models.UUIDField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['job', 'revision_number'],
                name='scout_job_revision_number',
            ),
        ]
        ordering = ['revision_number']


class DailySchedule(PublicIdMixin, models.Model):
    job = models.OneToOneField(Job, on_delete=models.CASCADE, related_name='daily_schedule')
    version = models.PositiveIntegerField(default=1)


class ScheduleRevision(models.Model):
    schedule = models.ForeignKey(DailySchedule, on_delete=models.CASCADE, related_name='revisions')
    number = models.PositiveIntegerField()
    plan_revision = models.ForeignKey(JobRevision, on_delete=models.CASCADE)
    plan_hash = models.CharField(max_length=64)
    local_time = models.TimeField()
    timezone = models.CharField(max_length=80)
    enabled = models.BooleanField()
    effective_from = models.DateTimeField()
    effective_until = models.DateTimeField(null=True)
    first_local_date = models.DateField(null=True)
    next_local_date = models.DateField(null=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['schedule', 'number'], name='scout_schedule_revision'),
        ]


class ScheduledOccurrence(PublicIdMixin, models.Model):
    schedule = models.ForeignKey(DailySchedule, on_delete=models.CASCADE, related_name='occurrences')
    revision = models.ForeignKey(ScheduleRevision, on_delete=models.CASCADE)
    local_date = models.DateField()
    scheduled_at = models.DateTimeField(null=True)
    processed_at = models.DateTimeField()
    status = models.CharField(max_length=32)
    reason = models.CharField(max_length=64, blank=True, default='')
    run = models.OneToOneField('Run', on_delete=models.CASCADE, null=True, related_name='scheduled_occurrence')

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['schedule', 'local_date'], name='scout_schedule_day'),
        ]
        indexes = [models.Index(fields=['status'], name='scout_occurrence_status')]


class ExtractionSchemaRevision(PublicIdMixin, models.Model):
    job_revision = models.OneToOneField(
        JobRevision,
        on_delete=models.CASCADE,
        related_name='schema_revision',
    )
    fields = models.JSONField(default=list)
    content_hash = models.CharField(max_length=64)


class ExtractionRulesRevision(PublicIdMixin, models.Model):
    job_revision = models.OneToOneField(
        JobRevision,
        on_delete=models.CASCADE,
        related_name='rules_revision',
    )
    rules = models.JSONField(default=list)
    content_hash = models.CharField(max_length=64)


class Run(PublicIdMixin, models.Model):
    job_revision = models.ForeignKey(JobRevision, on_delete=models.CASCADE, related_name='runs')
    user_public_id = models.UUIDField(db_index=True)
    status = models.CharField(max_length=32, db_index=True)
    error_summary = models.TextField(blank=True, default='')
    stop_reason = models.CharField(max_length=32, blank=True, default='')
    events = models.JSONField(default=list)
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    dispatch_requested_until = models.DateTimeField(null=True, blank=True)
    results_availability = models.CharField(max_length=12, default='available')
    retained_counts = models.JSONField(default=dict)
    retention_hold = models.CharField(max_length=8, blank=True, default='')
    retention_hold_version = models.PositiveIntegerField(default=1)

    class Meta:
        ordering = ['-created_at']


class Task(PublicIdMixin, models.Model):
    run = models.ForeignKey(Run, on_delete=models.CASCADE, related_name='tasks')
    kind = models.CharField(max_length=16)
    status = models.CharField(max_length=32, db_index=True)
    idempotency_key = models.CharField(max_length=200)
    payload = models.JSONField(default=dict)
    fencing_token = models.PositiveIntegerField(default=0)
    attempt_count = models.PositiveIntegerField(default=0)
    lease_until = models.DateTimeField(null=True, blank=True)
    published_at = models.DateTimeField(null=True, blank=True)
    next_retry_at = models.DateTimeField(null=True, blank=True)
    error_code = models.CharField(max_length=64, blank=True, default='')
    error_message = models.CharField(max_length=400, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['run', 'idempotency_key'],
                name='scout_task_idempotency',
            ),
        ]


class Attempt(PublicIdMixin, models.Model):
    task = models.ForeignKey(Task, on_delete=models.CASCADE, related_name='attempts')
    fencing_token = models.PositiveIntegerField()
    status = models.CharField(max_length=32)
    error_code = models.CharField(max_length=64, blank=True, default='')
    error_message = models.CharField(max_length=400, blank=True, default='')
    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)


class PageSnapshot(PublicIdMixin, models.Model):
    run = models.ForeignKey(Run, on_delete=models.CASCADE, related_name='snapshots')
    source_url = models.TextField()
    final_url = models.TextField(blank=True, default='')
    http_status = models.PositiveIntegerField(default=0)
    content_hash = models.CharField(max_length=64)
    content_path = models.CharField(max_length=300)
    byte_size = models.PositiveIntegerField(default=0)
    content_type = models.CharField(max_length=120, blank=True, default='')
    charset = models.CharField(max_length=40, blank=True, default='', db_default='')
    has_crawl_links = models.BooleanField(null=True)
    html_availability = models.CharField(max_length=12, default='available')
    purged_at = models.DateTimeField(null=True)
    fetched_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['run', 'source_url', 'content_hash'],
                name='scout_snapshot_run_url_hash',
            ),
        ]


class VisualSnapshot(PublicIdMixin, models.Model):
    user_public_id = models.UUIDField()
    proxy_profile_revision_public_id = models.UUIDField(null=True, blank=True)
    source_url = models.CharField(max_length=2000)
    final_url = models.TextField(blank=True, default='')
    status = models.CharField(max_length=12, default='queued')
    body = models.BinaryField(default=bytes)
    charset = models.CharField(max_length=40, blank=True, default='')
    error_code = models.CharField(max_length=64, blank=True, default='')
    error_detail = models.CharField(max_length=400, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)


class ProxyProfile(PublicIdMixin, models.Model):
    user_public_id = models.UUIDField(db_index=True)
    revoked_at = models.DateTimeField(null=True, blank=True)


class ProxyProfileRevision(PublicIdMixin, models.Model):
    profile = models.ForeignKey(ProxyProfile, on_delete=models.CASCADE, related_name='revisions')
    ip = models.GenericIPAddressField()
    port = models.PositiveIntegerField()
    auth_type = models.CharField(max_length=5, choices=[('basic', 'Basic'), ('none', 'Без авторизации')], default='basic')
    key_id = models.CharField(max_length=80, editable=False)
    ciphertext = models.TextField(blank=True, editable=False)


class Record(PublicIdMixin, models.Model):
    run = models.ForeignKey(Run, on_delete=models.CASCADE, related_name='records')
    snapshot = models.ForeignKey(PageSnapshot, on_delete=models.CASCADE, related_name='records')
    schema_revision = models.ForeignKey(
        ExtractionSchemaRevision,
        on_delete=models.CASCADE,
        related_name='records',
    )
    rules_revision = models.ForeignKey(
        ExtractionRulesRevision,
        on_delete=models.CASCADE,
        related_name='records',
    )
    identity_key = models.CharField(max_length=80)
    entity_key = models.CharField(max_length=64, null=True, blank=True)
    observation_index = models.PositiveIntegerField(null=True, blank=True)
    identity_state = models.JSONField(null=True, blank=True)
    values = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=['run', 'created_at', 'id'], name='scout_record_run_order')]
        constraints = [
            models.UniqueConstraint(
                fields=['snapshot', 'schema_revision', 'rules_revision', 'identity_key'],
                name='scout_record_identity',
            ),
            models.UniqueConstraint(
                fields=['run', 'entity_key'],
                condition=models.Q(entity_key__isnull=False),
                name='scout_record_run_entity',
            ),
        ]


class Evidence(PublicIdMixin, models.Model):
    record = models.ForeignKey(Record, on_delete=models.CASCADE, related_name='evidence')
    snapshot = models.ForeignKey(PageSnapshot, on_delete=models.CASCADE, related_name='evidence')
    field_path = models.CharField(max_length=80)
    source_url = models.TextField()
    locator = models.TextField()
    raw_value = models.TextField(blank=True, default='')
    normalized_value = models.TextField(blank=True, default='')
    extractor_id = models.CharField(max_length=80)
    extractor_version = models.CharField(max_length=32)
    observation_index = models.PositiveIntegerField(null=True, blank=True)
    typed_value = models.JSONField(null=True, blank=True)
    extracted_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['record', 'snapshot', 'observation_index', 'field_path', 'locator'],
                condition=models.Q(observation_index__isnull=False),
                name='scout_evidence_observation',
            ),
        ]


class CrawlURL(PublicIdMixin, models.Model):
    run = models.ForeignKey(Run, on_delete=models.CASCADE, related_name='crawl_urls')
    original_url = models.TextField()
    normalized_url = models.TextField(blank=True, default='')
    url_key = models.CharField(max_length=64)
    depth = models.PositiveIntegerField(default=0)
    parent = models.ForeignKey(
        'self',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='children',
    )
    state = models.CharField(max_length=16, db_index=True)
    skip_reason = models.CharField(max_length=80, blank=True, default='')
    snapshot = models.ForeignKey(
        PageSnapshot,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='crawl_urls',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['run', 'url_key'],
                name='scout_crawl_url_key',
            ),
        ]


class ExportArtifact(PublicIdMixin, models.Model):
    run = models.ForeignKey(Run, on_delete=models.CASCADE, related_name='exports')
    kind = models.CharField(max_length=8)
    storage_path = models.CharField(max_length=300)
    created_at = models.DateTimeField(auto_now_add=True)
    availability = models.CharField(max_length=12, default='available')
    byte_size = models.PositiveBigIntegerField(null=True)


class RetentionPolicy(models.Model):
    user_public_id = models.UUIDField(unique=True)
    version = models.PositiveIntegerField(default=1)
    automatic_enabled = models.BooleanField(default=False)
    html_days = models.PositiveIntegerField(default=30, null=True)
    result_days = models.PositiveIntegerField(default=365, null=True)
    export_days = models.PositiveIntegerField(default=7, null=True)
    keep_latest_results = models.PositiveIntegerField(default=2)
    last_automatic_date = models.DateField(null=True)


class RetentionPreview(PublicIdMixin, models.Model):
    user_public_id = models.UUIDField(db_index=True)
    policy_version = models.PositiveIntegerField()
    evaluated_at = models.DateTimeField()
    expires_at = models.DateTimeField()
    selection = models.JSONField()
    manifest = models.JSONField()
    manifest_hash = models.CharField(max_length=64)
    summary = models.JSONField()


class RetentionCleanup(PublicIdMixin, models.Model):
    preview = models.OneToOneField(RetentionPreview, on_delete=models.CASCADE, related_name='cleanup')
    user_public_id = models.UUIDField(db_index=True)
    idempotency_key = models.UUIDField()
    request_hash = models.CharField(max_length=64)
    status = models.CharField(max_length=24, default='queued', db_index=True)
    trigger = models.CharField(max_length=12, default='manual')
    created_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True)

    class Meta:
        constraints = [models.UniqueConstraint(
            fields=['user_public_id', 'idempotency_key'], name='scout_cleanup_request',
        )]


class RetentionCleanupItem(models.Model):
    cleanup = models.ForeignKey(RetentionCleanup, on_delete=models.CASCADE, related_name='items')
    number = models.PositiveIntegerField()
    specification = models.JSONField()
    status = models.CharField(max_length=12, default='pending')
    reason = models.CharField(max_length=64, blank=True, default='')
    deleted_bytes = models.PositiveBigIntegerField(default=0)
    deleted_rows = models.PositiveIntegerField(default=0)
    next_retry_at = models.DateTimeField(null=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['cleanup', 'number'], name='scout_cleanup_item')]
        ordering = ['number']


class RetentionLease(models.Model):
    token = models.UUIDField(unique=True)
    resource = models.CharField(max_length=100, db_index=True)
    expires_at = models.DateTimeField()


class RetentionHoldRequest(models.Model):
    user_public_id = models.UUIDField()
    key = models.UUIDField()
    request_hash = models.CharField(max_length=64)
    response = models.JSONField()

    class Meta:
        constraints = [models.UniqueConstraint(
            fields=['user_public_id', 'key'], name='scout_hold_request',
        )]
