"""Окончательные отказы загрузки, после которых очередь источника не продолжают."""

from .challenges import CHALLENGE_FAILURES
from .proxy_profiles import PROXY_FAILURES

STOP_FETCH_FAILURES = PROXY_FAILURES | CHALLENGE_FAILURES
