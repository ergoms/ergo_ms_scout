"""Операции моста. Обычный сбор не требует других модулей."""

from django.utils.translation import gettext as _

from src.core.integrations import bridge
from src.core.integrations.module_contracts import (
    AUDIT_ACTION_DEFINITIONS_GROUP,
    CORE_USER_DELETE,
)

from .services.errors import ScoutError


@bridge.provide_op('scout.health')
def _health(**_kwargs):
    return {'status': 'ok', 'module': 'scout'}


@bridge.subscribe_to(CORE_USER_DELETE)
def _on_user_delete(*, user_public_id=None, **_kwargs):
    if not user_public_id:
        return
    from .models import Job

    Job.objects.filter(user_public_id=user_public_id).delete()


bridge.provide_many(
    AUDIT_ACTION_DEFINITIONS_GROUP,
    key='scout',
    obj={
        'module': 'scout',
        'module_label': 'SCOUT',
        'actions': [
            {
                'action': 'scout.run.approved',
                'label': 'План сбора подтверждён',
                'icon': 'Check',
                'category': 'run',
                'category_label': 'Сбор',
                'severity': 'info',
            },
            {
                'action': 'scout.run.started',
                'label': 'Сбор запущен',
                'icon': 'Play',
                'category': 'run',
                'category_label': 'Сбор',
                'severity': 'info',
            },
            {
                'action': 'scout.export.created',
                'label': 'Выгрузка создана',
                'icon': 'Download',
                'category': 'export',
                'category_label': 'Выгрузка',
                'severity': 'info',
            },
        ],
    },
)


def audit_run(action: str, request, run, label: str) -> None:
    from src.core.integrations.module_contracts import AUDIT_RECORD

    try:
        bridge.call(
            AUDIT_RECORD,
            action=action,
            source_module='scout',
            request=request,
            entity={'type': 'run', 'ref': str(run.public_id), 'label': label},
        )
    except Exception:
        return


def error_status(exc: ScoutError) -> int:
    if exc.code in ('approval_required', 'slot_busy', 'plan_changed', 'invalid_state'):
        return 409
    if exc.code == 'media':
        return 503
    return 400


def error_payload(exc: ScoutError) -> dict:
    return {'code': exc.code, 'detail': str(exc) or _('Ошибка сбора')}
