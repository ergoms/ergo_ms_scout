from django.urls import path

from .views import (
    HealthView,
    JobDetailView,
    JobExploreView,
    JobListCreateView,
    RunApproveView,
    RunCancelView,
    RunDetailView,
    RunExportView,
    RunPauseView,
    RunRecordsView,
    RunResumeView,
    RunStartView,
)

urlpatterns = [
    path('health/', HealthView.as_view(), name='scout-health'),
    path('jobs/', JobListCreateView.as_view(), name='scout-jobs'),
    path('jobs/<uuid:public_id>/', JobDetailView.as_view(), name='scout-job'),
    path('jobs/<uuid:public_id>/explore/', JobExploreView.as_view(), name='scout-job-explore'),
    path('runs/<uuid:public_id>/', RunDetailView.as_view(), name='scout-run'),
    path('runs/<uuid:public_id>/approve/', RunApproveView.as_view(), name='scout-run-approve'),
    path('runs/<uuid:public_id>/start/', RunStartView.as_view(), name='scout-run-start'),
    path('runs/<uuid:public_id>/pause/', RunPauseView.as_view(), name='scout-run-pause'),
    path('runs/<uuid:public_id>/resume/', RunResumeView.as_view(), name='scout-run-resume'),
    path('runs/<uuid:public_id>/cancel/', RunCancelView.as_view(), name='scout-run-cancel'),
    path('runs/<uuid:public_id>/records/', RunRecordsView.as_view(), name='scout-run-records'),
    path('runs/<uuid:public_id>/export/', RunExportView.as_view(), name='scout-run-export'),
]
