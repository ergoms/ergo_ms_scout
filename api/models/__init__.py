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
    fetched_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['run', 'source_url', 'content_hash'],
                name='scout_snapshot_run_url_hash',
            ),
        ]


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
    values = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['snapshot', 'schema_revision', 'rules_revision', 'identity_key'],
                name='scout_record_identity',
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
    extracted_at = models.DateTimeField(auto_now_add=True)


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
