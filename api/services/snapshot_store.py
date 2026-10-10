"""Неизменяемые снимки HTML в кэше проекта, не в каталоге исходников."""

import hashlib
import os
from pathlib import Path

from src.config.paths import CACHE_DIR

_ROOT = CACHE_DIR / 'scout' / 'snapshots'


def snapshot_root() -> Path:
    _ROOT.mkdir(parents=True, exist_ok=True)
    return _ROOT


def store_snapshot_bytes(body: bytes) -> tuple[str, str]:
    digest = hashlib.sha256(body).hexdigest()
    relative = f'scout/snapshots/{digest}.html'
    target = CACHE_DIR / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        temporary = target.with_suffix('.html.partial')
        temporary.write_bytes(body)
        os.replace(temporary, target)
    return digest, relative


def read_snapshot_bytes(relative_path: str) -> bytes:
    root = (CACHE_DIR / 'scout' / 'snapshots').resolve()
    path = (CACHE_DIR / relative_path).resolve()
    if root not in path.parents and path != root:
        raise ValueError('snapshot path escaped cache')
    return path.read_bytes()
