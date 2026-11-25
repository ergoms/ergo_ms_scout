from django.urls import path
from .history_views import JobRunHistoryView

from .proxy_views import ProxyProfileCheckView, ProxyProfilesView, ProxyProfileView
from .tree_views import VisualImageView, VisualTreeView, VisualTreeNodeView, VisualTreeSearchView
from .schedule_views import DailyScheduleView, ScheduleOccurrencesView
from .retention_views import (
    RetentionPolicyView, RunRetentionView, RetentionHoldView, RetentionPreviewsView,
    RetentionPreviewItemsView, RetentionCleanupsView, RetentionCleanupView, RetentionCleanupItemsView,
)
from .views import (
    HealthView,
    JobDetailView,
    JobExploreView,
    JobListCreateView,
    RunApproveView,
    RunCancelView,
    RunComparisonView,
    RunDetailView,
    RunExportView,
    RunImageView,
    RunPauseView,
    RunRecordsView,
    RunResumeView,
    RunStartView,
)
from .visual_views import (
    PlanSuggestionView,
    VisualSnapshotCheckView,
    VisualSnapshotCreateView,
    VisualSnapshotDetailView,
)

urlpatterns = [
    path('retention/policy/', RetentionPolicyView.as_view()),
    path('retention/previews/', RetentionPreviewsView.as_view()),
    path('retention/previews/<uuid:public_id>/items/', RetentionPreviewItemsView.as_view()),
    path('retention/cleanups/', RetentionCleanupsView.as_view()),
    path('retention/cleanups/<uuid:public_id>/', RetentionCleanupView.as_view()),
    path('retention/cleanups/<uuid:public_id>/items/', RetentionCleanupItemsView.as_view()),
    path('runs/<uuid:public_id>/retention/', RunRetentionView.as_view()),
    path('runs/<uuid:public_id>/retention/hold/', RetentionHoldView.as_view()),
    path('health/', HealthView.as_view(), name='scout-health'),
    path('proxy-profiles/', ProxyProfilesView.as_view(), name='scout-proxy-profiles'),
    path('proxy-profiles/<uuid:public_id>/', ProxyProfileView.as_view(), name='scout-proxy-profile'),
    path('proxy-profiles/<uuid:public_id>/check/', ProxyProfileCheckView.as_view(), name='scout-proxy-check'),
    path('visual-snapshots/', VisualSnapshotCreateView.as_view(), name='scout-visual-snapshots'),
    path('visual-snapshots/<uuid:public_id>/image/', VisualImageView.as_view(), name='scout-visual-image'),
    path('visual-snapshots/<uuid:public_id>/tree/', VisualTreeView.as_view(), name='scout-visual-tree'),
    path('visual-snapshots/<uuid:public_id>/tree/nodes/<str:node_id>/', VisualTreeNodeView.as_view(), name='scout-visual-tree-node'),
    path('visual-snapshots/<uuid:public_id>/tree/search/', VisualTreeSearchView.as_view(), name='scout-visual-tree-search'),
    path('visual-snapshots/<uuid:public_id>/', VisualSnapshotDetailView.as_view(), name='scout-visual-snapshot'),
    path('visual-snapshots/<uuid:public_id>/check/', VisualSnapshotCheckView.as_view(), name='scout-visual-check'),
    path('plan-suggestions/', PlanSuggestionView.as_view(), name='scout-plan-suggestions'),
    path('jobs/', JobListCreateView.as_view(), name='scout-jobs'),
    path('jobs/<uuid:public_id>/', JobDetailView.as_view(), name='scout-job'),
    path('jobs/<uuid:public_id>/runs/', JobRunHistoryView.as_view(), name='scout-job-runs'),
    path('jobs/<uuid:public_id>/explore/', JobExploreView.as_view(), name='scout-job-explore'),
    path('jobs/<uuid:public_id>/schedule/', DailyScheduleView.as_view(), name='scout-daily-schedule'),
    path('jobs/<uuid:public_id>/schedule/occurrences/', ScheduleOccurrencesView.as_view(), name='scout-schedule-days'),
    path('runs/<uuid:public_id>/', RunDetailView.as_view(), name='scout-run'),
    path('runs/<uuid:public_id>/approve/', RunApproveView.as_view(), name='scout-run-approve'),
    path('runs/<uuid:public_id>/start/', RunStartView.as_view(), name='scout-run-start'),
    path('runs/<uuid:public_id>/pause/', RunPauseView.as_view(), name='scout-run-pause'),
    path('runs/<uuid:public_id>/resume/', RunResumeView.as_view(), name='scout-run-resume'),
    path('runs/<uuid:public_id>/cancel/', RunCancelView.as_view(), name='scout-run-cancel'),
    path('runs/<uuid:public_id>/records/', RunRecordsView.as_view(), name='scout-run-records'),
    path('runs/<uuid:public_id>/images/<uuid:image_id>/', RunImageView.as_view(), name='scout-run-image'),
    path('runs/<uuid:public_id>/compare/', RunComparisonView.as_view(), name='scout-run-comparison'),
    path('runs/<uuid:public_id>/export/', RunExportView.as_view(), name='scout-run-export'),
]
