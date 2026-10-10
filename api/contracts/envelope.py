"""Нейтральный конверт операции. Первая реализация вызывает его внутри процесса."""

from ..services.constants import EXTRACTOR_ID, EXTRACTOR_VERSION, PROTOCOL_VERSION


def build_extract_request(rules: list[dict]) -> dict:
    return {
        'protocol_version': PROTOCOL_VERSION,
        'operation': 'extract',
        'plugin_id': EXTRACTOR_ID,
        'plugin_version': EXTRACTOR_VERSION,
        'config': {'rules': rules},
        'input_artifacts': [{'type': 'page_snapshot'}],
    }
