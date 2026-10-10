"""Права SCOUT. Глобальный администратор проходит через PermissionService."""

from src.core.cms.adp.base_permissions import BaseModulePermission

MODULE_NAME = 'scout'

SCOUT_VIEW = 'scout_view'
SCOUT_CONFIGURE = 'scout_configure'
SCOUT_RUN = 'scout_run'
SCOUT_REVIEW = 'scout_review'
SCOUT_EXPORT = 'scout_export'


class _ScoutPermission(BaseModulePermission):
    module_name = MODULE_NAME


class CanViewScout(_ScoutPermission):
    required_permission = SCOUT_VIEW


class CanConfigureScout(_ScoutPermission):
    required_permission = SCOUT_CONFIGURE


class CanRunScout(_ScoutPermission):
    required_permission = SCOUT_RUN


class CanReviewScout(_ScoutPermission):
    required_permission = SCOUT_REVIEW


class CanExportScout(_ScoutPermission):
    required_permission = SCOUT_EXPORT
