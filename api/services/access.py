"""Кто видит задания. Чужой сбор не отдаётся даже по public_id."""

from django.utils.translation import gettext as _

from src.core.cms.adp.services.permissions import PermissionService

from ..models import Job, Run
from .errors import ScoutError


def require_user_public_id(user):
    public_id = getattr(user, 'public_id', None)
    if not public_id:
        raise ScoutError('invalid', _('У пользователя нет public_id'))
    return public_id


def jobs_for(user):
    queryset = Job.objects.all()
    if PermissionService.is_admin(user):
        return queryset
    public_id = getattr(user, 'public_id', None)
    if not public_id:
        return queryset.none()
    return queryset.filter(user_public_id=public_id)


def runs_for(user):
    queryset = Run.objects.select_related('job_revision', 'job_revision__job')
    if PermissionService.is_admin(user):
        return queryset
    public_id = getattr(user, 'public_id', None)
    if not public_id:
        return queryset.none()
    return queryset.filter(user_public_id=public_id)
