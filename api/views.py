"""Служебное API редактора сбора."""

from uuid import UUID

from django.db.models import Q
from django.shortcuts import get_object_or_404
from django.utils.translation import gettext as _
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
from .present import job_payload, record_payload, records_with_evidence, run_payload
from .services.access import jobs_for, require_user_public_id, runs_for
from .services.constants import RUN_AWAITING
from .services.errors import ScoutError
from .services.exporting import export_run
from .services.plan import IDENTITY_UNSET
from .services.preview import page_type_preview, preview_rows
from .services.run_comparison import compare_runs
from .services.retention_locks import result_response
from .services.runs import (
    approve_run,
    cancel_run,
    create_job,
    explore_job,
    pause_run,
    resume_run,
    revise_job,
    start_run,
)
from .services.tasks_exec import schedule_publish
from .services.value_types import use_typed_values


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
            data = _request_object(request.data)
            job = create_job(
                user_public_id=user_id,
                title=data.get('title'),
                seed_urls=data.get('seed_urls'),
                fields=data.get('fields'),
                record=data.get('record'),
                crawl=data.get('crawl'),
                identity=data.get('identity', IDENTITY_UNSET),
                proxy_profile_revision_public_id=data.get('proxy_profile_revision_public_id'),
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
            data = _request_object(request.data)
            revise_job(
                job=job,
                seed_urls=data.get('seed_urls'),
                fields=data.get('fields'),
                record=data.get('record'),
                title=data.get('title'),
                crawl=data.get('crawl'),
                identity=data.get('identity', IDENTITY_UNSET),
                **({'proxy_profile_revision_public_id': data['proxy_profile_revision_public_id']}
                   if 'proxy_profile_revision_public_id' in data else {}),
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
        try:
            typed = use_typed_values(request.query_params.get('value_format'))
        except ScoutError as exc:
            return Response(error_payload(exc), status=error_status(exc))
        runs = runs_for(request.user)
        if request.query_params.get('summary') == '1':
            run = get_object_or_404(runs, public_id=public_id)
            return Response(run_payload(run, summary=True))
        run = get_object_or_404(
            runs.prefetch_related('tasks', 'snapshots'),
            public_id=public_id,
        )
        return Response(_run_body(run, typed=typed))


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
        return Response(_run_body(run))


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


class RunResumeView(BaseAPIView):
    permission_classes = [IsAuthenticated, CanRunScout]

    def post(self, request, public_id):
        run = get_object_or_404(runs_for(request.user), public_id=public_id)
        try:
            run = resume_run(run=run)
        except ScoutError as exc:
            return Response(error_payload(exc), status=error_status(exc))
        schedule_publish(run.public_id)
        return Response(run_payload(run), status=202)


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

    @result_response
    def get(self, request, public_id):
        run = get_object_or_404(runs_for(request.user), public_id=public_id)
        try:
            typed = use_typed_values(request.query_params.get('value_format'))
        except ScoutError as exc:
            return Response(error_payload(exc), status=error_status(exc))
        order = request.query_params.get('order', 'oldest')
        if order not in ('oldest', 'recent'):
            exc = ScoutError('invalid_order', _('Порядок записей: oldest или recent.'))
            return Response(error_payload(exc), status=error_status(exc))
        recent = order == 'recent'
        records = run.records.order_by(*(['-created_at', '-pk'] if recent else ['created_at', 'pk']))
        if not recent and 'limit' not in request.query_params and 'cursor' not in request.query_params:
            return Response({'records': [
                record_payload(item, typed=typed) for item in records_with_evidence(records, typed=typed)
            ]})

        try:
            limit = int(request.query_params.get('limit', '50'))
        except ValueError:
            limit = 0
        if not 1 <= limit <= 200:
            exc = ScoutError('invalid_limit', _('Размер порции должен быть целым числом от 1 до 200.'))
            return Response(error_payload(exc), status=error_status(exc))

        if 'cursor' in request.query_params:
            try:
                after = records.only('created_at').get(public_id=UUID(request.query_params['cursor']))
            except (ValueError, records.model.DoesNotExist):
                exc = ScoutError('invalid_cursor', _('Курсор не соответствует записи этого сбора.'))
                return Response(error_payload(exc), status=error_status(exc))
            boundary = 'lt' if recent else 'gt'
            records = records.filter(
                Q(**{f'created_at__{boundary}': after.created_at})
                | Q(created_at=after.created_at, **{f'pk__{boundary}': after.pk}),
            )

        page = list(records_with_evidence(records, typed=typed)[:limit + 1])
        return Response({
            'records': [record_payload(item, typed=typed) for item in page[:limit]],
            'next_cursor': str(page[limit - 1].public_id) if len(page) > limit else None,
        })


class RunComparisonView(BaseAPIView):
    permission_classes = [IsAuthenticated, CanViewScout]

    def get(self, request, public_id):
        runs = runs_for(request.user)
        target = get_object_or_404(runs, public_id=public_id)
        query = request.query_params
        try:
            try:
                base_id = UUID(query.get('base_run_public_id', ''))
            except ValueError as exc:
                raise ScoutError('invalid_comparison', _('Укажите UUID исходного запуска')) from exc
            base = get_object_or_404(runs, public_id=base_id)
            try:
                limit = int(query.get('limit', '50'))
            except ValueError:
                limit = 0
            if not 1 <= limit <= 200:
                raise ScoutError('invalid_limit', _('Размер порции должен быть целым числом от 1 до 200.'))
            result = compare_runs(base, target, field=query.get('field'), limit=limit, cursor=query.get('cursor'))
        except ScoutError as exc:
            return Response(error_payload(exc), status=error_status(exc))
        return Response(result)


class RunExportView(BaseAPIView):
    permission_classes = [IsAuthenticated, CanExportScout]

    def post(self, request, public_id):
        run = get_object_or_404(runs_for(request.user), public_id=public_id)
        try:
            data = _request_object(request.data)
            kind = str(data.get('kind') or 'json')
            typed = use_typed_values(data.get('value_format'))
            _artifact, url, filename = export_run(run=run, kind=kind, typed=typed)
        except ScoutError as exc:
            return Response(error_payload(exc), status=error_status(exc))
        audit_run('scout.export.created', request, run, filename)
        return Response({'download_url': url, 'filename': filename, 'kind': kind})


def _request_object(data):
    if not isinstance(data, dict):
        raise ScoutError('invalid', _('Тело запроса должно быть объектом'), path='/')
    return data


def _run_body(run, *, typed: bool = False):
    preview = None
    diagnostics = None
    if run.status == RUN_AWAITING:
        try:
            if (run.job_revision.crawl or {}).get('page_types'):
                preview, diagnostics = page_type_preview(run, typed=typed)
            else:
                preview = preview_rows(run, typed=typed)
        except ScoutError as exc:
            preview = {'error': error_payload(exc)}
    return run_payload(run, preview=preview, page_type_preview=diagnostics)
