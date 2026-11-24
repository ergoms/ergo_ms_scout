"""Чтение DOM-метаданных: владельцы, сроки, пределы и безопасные ошибки."""

import json
import logging
import math
from contextlib import contextmanager

from django.db import DatabaseError
from django.http import HttpResponse
from django.utils import timezone
from django.views.decorators.debug import sensitive_variables
from rest_framework import exceptions
from rest_framework.permissions import IsAuthenticated
from rest_framework.renderers import JSONRenderer
from rest_framework.response import Response
from rest_framework.throttling import UserRateThrottle

from src.core.utils.base.base_views import BaseAPIView

from .models import VisualSnapshot
from .permissions import CanConfigureScout
from .services.errors import ScoutError
from .services.http_fetch import IMAGE_EXTENSIONS
from .services.retention_locks import read_resource
from .services.visual_rules import VisualDocument
from .services.visual_images import download_image, image_url
from .services.visual_snapshots import SNAPSHOT_TTL, snapshot_status
from .services.visual_tree import (
    MISSING, MAX_RESPONSE_BYTES, VisualTree, check_tree_version, invalid, node_id, search_terms, validate_keys,
)

logger = logging.getLogger('modules.scout')
_STATUSES = {
    'snapshot_not_found': 404, 'node_not_found': 404,
    'snapshot_not_ready': 409, 'tree_version_changed': 409,
    'invalid_query': 400, 'node_not_child': 400, 'invalid_cursor': 400, 'cursor_expired': 400,
    'request_too_large': 413, 'unsupported_media_type': 415,
    'tree_too_large': 422, 'tree_depth_exceeded': 422, 'storage_unavailable': 503,
    'image_source_unavailable': 422, 'unsupported_image': 415, 'too_large': 413,
    'url_blocked': 422, 'image_fetch_failed': 502,
}


class _DOMReadThrottle(UserRateThrottle):
    scope = 'scout_dom_read'
    rate = '240/minute'


class _DOMSearchThrottle(UserRateThrottle):
    scope = 'scout_dom_search'
    rate = '60/minute'


def _query(request, allowed):
    if any(len(values) != 1 for _, values in request.query_params.lists()):
        raise invalid()
    data = request.query_params.dict()
    validate_keys(data, allowed)
    return data


class _SnapshotReadView(BaseAPIView):
    permission_classes = [IsAuthenticated, CanConfigureScout]
    throttle_classes = [*BaseAPIView.throttle_classes, _DOMReadThrottle]
    renderer_classes = [JSONRenderer]

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        response['Cache-Control'] = 'no-store'
        return response

    @sensitive_variables()
    def handle_exception(self, exc):
        headers = {}
        path = None
        if isinstance(exc, ScoutError) and exc.code in _STATUSES:
            code, status, detail, path = exc.code, _STATUSES[exc.code], str(exc), exc.path
        elif isinstance(exc, (exceptions.AuthenticationFailed, exceptions.NotAuthenticated)):
            code, status, detail = 'authentication_failed', 401, 'Требуется аутентификация'
            headers['WWW-Authenticate'] = 'Bearer'
        elif isinstance(exc, exceptions.PermissionDenied):
            code, status, detail = 'permission_denied', 403, 'Недостаточно прав'
        elif isinstance(exc, exceptions.Throttled):
            code, status, detail = 'throttled', 429, 'Слишком частые запросы'
            if exc.wait is not None:
                headers['Retry-After'] = str(math.ceil(exc.wait))
        elif isinstance(exc, exceptions.MethodNotAllowed):
            code, status, detail = 'method_not_allowed', 405, 'Метод не поддерживается'
        elif isinstance(exc, exceptions.NotAcceptable):
            code, status, detail = 'not_acceptable', 406, 'Доступен формат JSON'
        elif isinstance(exc, DatabaseError):
            code, status, detail = 'storage_unavailable', 503, 'Хранилище временно недоступно'
        else:
            # Нельзя записывать исключение: в нём могут быть HTML, поиск или курсор.
            logger.error('SCOUT не прочитал метаданные дерева')
            code, status, detail = 'internal_error', 500, 'Не удалось прочитать дерево'
        return Response({'code': code, 'detail': detail, **({'path': path} if path else {})}, status=status, headers=headers)

    def _snapshot(self, request, public_id):
        snapshot = VisualSnapshot.objects.defer('editor_html').filter(
            public_id=public_id, user_public_id=request.user.public_id,
            created_at__gt=timezone.now() - SNAPSHOT_TTL,
        ).first()
        if snapshot is None:
            raise ScoutError('snapshot_not_found', 'Снимок недоступен')
        return snapshot

    @contextmanager
    def _ready_snapshot(self, request, public_id):
        snapshot = self._snapshot(request, public_id)
        with read_resource(f'visual:{snapshot.pk}'):
            snapshot = self._snapshot(request, public_id)
            if snapshot_status(snapshot)[0] != 'ready':
                raise ScoutError('snapshot_not_ready', 'Снимок не готов для чтения')
            yield snapshot

    @staticmethod
    def _document(snapshot):
        if len(snapshot.body) > 1_000_000:
            raise ScoutError('tree_too_large', 'Снимок превышает допустимый объём')
        try:
            return VisualDocument(bytes(snapshot.body), snapshot.charset)
        except ScoutError as exc:
            if exc.code == 'too_large':
                raise ScoutError('tree_too_large', 'Слишком много узлов для дерева') from None
            raise


class _TreeView(_SnapshotReadView):
    @sensitive_variables()
    def _read(self, request, public_id, action):
        with self._ready_snapshot(request, public_id) as snapshot:
            data, operation = action()
            required = operation != 'branch' or any(k in data for k in ('parent_id', 'cursor', 'around_node_id'))
            check_tree_version(snapshot, data.get('tree_version', MISSING), required=required)
            tree = VisualTree(self._document(snapshot), snapshot)
            if operation == 'branch':
                result = tree.branch(data)
            elif operation == 'detail':
                result = tree.detail(self.kwargs['node_id'])
            else:
                query, tag = search_terms(data)
                result = tree.search(data, query, tag)
            if len(JSONRenderer().render(result)) > MAX_RESPONSE_BYTES:
                raise ScoutError('tree_too_large', 'Ответ дерева превышает допустимый объём')
            return Response(result)


class VisualImageView(_SnapshotReadView):
    http_method_names = ['get', 'options']

    def perform_content_negotiation(self, request, force=False):
        # HTTP-файл имеет MIME источника; ошибки всегда возвращаются в JSON.
        return super().perform_content_negotiation(request, force=True)

    @sensitive_variables()
    def get(self, request, public_id):
        with self._ready_snapshot(request, public_id) as snapshot:
            data = _query(request, ('tree_version', 'node_id'))
            check_tree_version(snapshot, data.get('tree_version', MISSING), required=True)
            uid = node_id(data.get('node_id'))
            url = image_url(self._document(snapshot), uid, snapshot.final_url)
            image = download_image(snapshot, url)
        media = image['content_type']
        return HttpResponse(image['body'], content_type=media, headers={
            'Content-Disposition': f'attachment; filename="scout-{uid}.{IMAGE_EXTENSIONS[media]}"',
            'X-Content-Type-Options': 'nosniff',
        })


class VisualTreeView(_TreeView):
    def get(self, request, public_id):
        return self._read(request, public_id, lambda: (
            _query(request, ('parent_id', 'around_node_id', 'limit', 'cursor', 'tree_version')), 'branch',
        ))


class VisualTreeNodeView(_TreeView):
    def get(self, request, public_id, node_id):
        return self._read(request, public_id, lambda: (_query(request, ('tree_version',)), 'detail'))


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise invalid()
        result[key] = value
    return result


class VisualTreeSearchView(_TreeView):
    throttle_classes = [*BaseAPIView.throttle_classes, _DOMSearchThrottle]

    @sensitive_variables()
    def post(self, request, public_id):
        def body():
            _query(request, ())
            if request.content_type != 'application/json':
                raise ScoutError('unsupported_media_type', 'Поиск принимает JSON')
            raw = request.stream.read(4097) if request.stream is not None else b''
            if len(raw) > 4096:
                raise ScoutError('request_too_large', 'Запрос поиска превышает допустимый объём')
            try:
                data = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique_object)
            except (ValueError, UnicodeError, RecursionError):
                raise invalid() from None
            validate_keys(data, ('q', 'tag', 'limit', 'cursor', 'tree_version'))
            return data, 'search'

        return self._read(request, public_id, body)
