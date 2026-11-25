from django.shortcuts import get_object_or_404
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from src.core.utils.base.base_views import BaseAPIView

from .integrations import error_payload, error_status
from .permissions import CanViewScout
from .services.access import jobs_for
from .services.errors import ScoutError
from .services.run_history import list_job_runs


class JobRunHistoryView(BaseAPIView):
    permission_classes = [IsAuthenticated, CanViewScout]
    http_method_names = ['get', 'head', 'options']

    def get(self, request, public_id):
        job = get_object_or_404(jobs_for(request.user), public_id=public_id)
        try:
            data = list_job_runs(request.user, job, limit=request.query_params.get('limit', '50'),
                                 cursor=request.query_params.get('cursor'))
        except ScoutError as error:
            return Response(error_payload(error), status=error_status(error))
        response = Response(data)
        response['Cache-Control'] = 'private, no-store'
        return response
