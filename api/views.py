"""Служебное API редактора сбора."""

from django.shortcuts import get_object_or_404
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from src.core.utils.base.base_views import BaseAPIView

from .integrations import audit_run, error_payload, error_status
from .permissions import (
    CanConfigureScout,
    CanExportScout,
    CanReviewScout,
    CanRunScout,
    CanViewScout,
)
from .present import job_payload, record_payload, run_payload
from .services.access import jobs_for, require_user_public_id, runs_for
from .services.constants import RUN_AWAITING
from .services.errors import ScoutError
from .services.exporting import export_run
from .services.preview import preview_rows
from .services.runs import (
    approve_run,
    cancel_run,
    create_job,
    explore_job,
    pause_run,
    revise_job,
    start_run,
)
from .services.tasks_exec import schedule_publish


class HealthView(BaseAPIView):
    def get(self, request):
        return Response({'status': 'ok', 'module': 'scout'})


class JobListCreateView(BaseAPIView):
    def get_permissions(self):
        if self.request.method == 'POST':
            return [IsAuthenticated(), CanConfigureScout()]
        return [IsAuthenticated(), CanViewScout()]

    def get(self, request):
        jobs = jobs_for(request.user).prefetch_related('revisions')
        return Response({'jobs': [job_payload(job) for job in jobs[:100]]})

    def post(self, request):
        try:
            user_id = require_user_public_id(request.user)
            job = create_job(
                user_public_id=user_id,
                title=request.data.get('title') or '',
                seed_urls=request.data.get('seed_urls') or [],
                fields=request.data.get('fields') or [],
            )
        except ScoutError as exc:
            return Response(error_payload(exc), status=error_status(exc))
        return Response(job_payload(job), status=201)


class JobDetailView(BaseAPIView):
    def get_permissions(self):
        if self.request.method == 'PATCH':
            return [IsAuthenticated(), CanConfigureScout()]
        return [IsAuthenticated(), CanViewScout()]

    def get(self, request, public_id):
        job = get_object_or_404(jobs_for(request.user), public_id=public_id)
        return Response(job_payload(job))

    def patch(self, request, public_id):
        job = get_object_or_404(jobs_for(request.user), public_id=public_id)
        try:
            revise_job(
                job=job,
                seed_urls=request.data.get('seed_urls') or [],
                fields=request.data.get('fields') or [],
            )
        except ScoutError as exc:
            return Response(error_payload(exc), status=error_status(exc))
        job.refresh_from_db()
        return Response(job_payload(job))


class JobExploreView(BaseAPIView):
    permission_classes = [IsAuthenticated, CanRunScout]

    def post(self, request, public_id):
        job = get_object_or_404(jobs_for(request.user), public_id=public_id)
        try:
            user_id = require_user_public_id(request.user)
            run = explore_job(job=job, user_public_id=user_id)
        except ScoutError as exc:
            return Response(error_payload(exc), status=error_status(exc))
        schedule_publish(run.public_id)
        return Response(run_payload(run), status=202)


class RunDetailView(BaseAPIView):
    permission_classes = [IsAuthenticated, CanViewScout]

    def get(self, request, public_id):
        run = get_object_or_404(
            runs_for(request.user).prefetch_related('tasks', 'snapshots'),
            public_id=public_id,
        )
        preview = None
        if run.status == RUN_AWAITING:
            try:
                preview = preview_rows(run)
            except ScoutError as exc:
                preview = {'error': error_payload(exc)}
        return Response(run_payload(run, preview=preview))


class RunApproveView(BaseAPIView):
    permission_classes = [IsAuthenticated, CanReviewScout]

    def post(self, request, public_id):
        run = get_object_or_404(runs_for(request.user), public_id=public_id)
        try:
            user_id = require_user_public_id(request.user)
            run = approve_run(run=run, user_public_id=user_id)
        except ScoutError as exc:
            return Response(error_payload(exc), status=error_status(exc))
        audit_run('scout.run.approved', request, run, run.job_revision.job.title)
        return Response(run_payload(run))


class RunStartView(BaseAPIView):
    permission_classes = [IsAuthenticated, CanRunScout]

    def post(self, request, public_id):
        run = get_object_or_404(runs_for(request.user), public_id=public_id)
        try:
            run = start_run(run=run)
        except ScoutError as exc:
            return Response(error_payload(exc), status=error_status(exc))
        schedule_publish(run.public_id)
        audit_run('scout.run.started', request, run, run.job_revision.job.title)
        return Response(run_payload(run), status=202)


class RunPauseView(BaseAPIView):
    permission_classes = [IsAuthenticated, CanRunScout]

    def post(self, request, public_id):
        run = get_object_or_404(runs_for(request.user), public_id=public_id)
        try:
            run = pause_run(run=run)
        except ScoutError as exc:
            return Response(error_payload(exc), status=error_status(exc))
        return Response(run_payload(run))


class RunCancelView(BaseAPIView):
    permission_classes = [IsAuthenticated, CanRunScout]

    def post(self, request, public_id):
        run = get_object_or_404(runs_for(request.user), public_id=public_id)
        try:
            run = cancel_run(run=run)
        except ScoutError as exc:
            return Response(error_payload(exc), status=error_status(exc))
        return Response(run_payload(run))


class RunRecordsView(BaseAPIView):
    permission_classes = [IsAuthenticated, CanViewScout]

    def get(self, request, public_id):
        run = get_object_or_404(runs_for(request.user), public_id=public_id)
        records = run.records.prefetch_related('evidence').order_by('created_at')
        return Response({'records': [record_payload(item) for item in records]})


class RunExportView(BaseAPIView):
    permission_classes = [IsAuthenticated, CanExportScout]

    def post(self, request, public_id):
        run = get_object_or_404(runs_for(request.user), public_id=public_id)
        kind = str(request.data.get('kind') or 'json')
        try:
            _artifact, url, filename = export_run(run=run, kind=kind)
        except ScoutError as exc:
            return Response(error_payload(exc), status=error_status(exc))
        audit_run('scout.export.created', request, run, filename)
        return Response({'download_url': url, 'filename': filename, 'kind': kind})
