"""Выбор модели Scout не зависит от модели по умолчанию других потребителей."""

from unittest.mock import patch

from django.test import SimpleTestCase, override_settings

from ..services.errors import ScoutError
from ..services.plan_suggestions import _generate


@override_settings(SCOUT_AI_LOCAL_ONLY=False)
class ScoutAIModelTests(SimpleTestCase):
    def test_explicit_model_uses_provider_profile_and_blank_preserves_default(self):
        with patch('modules.scout.api.services.plan_suggestions.bridge') as provider:
            provider.call.return_value='{}'
            with override_settings(SCOUT_AI_MODEL=' qwen3.5:4b '):
                self.assertEqual(_generate('sample'),'{}')
                self.assertEqual(provider.call.call_args.kwargs['config']['model'],'qwen3.5:4b')
                self.assertTrue(provider.call.call_args.kwargs['skip_env_injection'])
            with override_settings(SCOUT_AI_MODEL=''):
                _generate('sample')
                self.assertNotIn('model',provider.call.call_args.kwargs['config'])
                self.assertFalse(provider.call.call_args.kwargs['skip_env_injection'])

    @override_settings(SCOUT_AI_LOCAL_ONLY=True, BRIDGE_TRANSPORT='local')
    def test_strict_local_mode_requires_explicit_model_and_reaches_provider(self):
        with patch('modules.scout.api.services.plan_suggestions.bridge') as provider:
            provider.call.return_value = '{}'
            with override_settings(SCOUT_AI_MODEL='qwen3.5:4b'):
                _generate('sample')
                self.assertEqual(provider.call.call_args.args, ('ollama_framework.chat_local',))
                self.assertEqual(provider.call.call_args.kwargs['model'], 'qwen3.5:4b')
            provider.reset_mock()
            with override_settings(SCOUT_AI_MODEL=''):
                with self.assertRaises(ScoutError):
                    _generate('sample')
            provider.call.assert_not_called()

    @override_settings(SCOUT_AI_LOCAL_ONLY=True, SCOUT_AI_MODEL='qwen3.5:4b', BRIDGE_TRANSPORT='http')
    def test_strict_local_mode_blocks_http_bridge_before_sending_prompt(self):
        with patch('modules.scout.api.services.plan_suggestions.bridge') as provider:
            with self.assertRaises(ScoutError):
                _generate('private sample')
            provider.call.assert_not_called()

    def test_bad_server_model_does_not_call_provider(self):
        with patch('modules.scout.api.services.plan_suggestions.bridge') as provider:
            for model in (None,'name\nother','x'*201):
                with override_settings(SCOUT_AI_MODEL=model):
                    with self.assertRaises(ScoutError) as failure:
                        _generate('sample')
                    self.assertEqual(failure.exception.code,'model_unavailable')
            provider.call.assert_not_called()


class ScoutAISampleBudgetTests(SimpleTestCase):
    def test_indentation_does_not_hide_fields_beyond_prompt_budget(self):
        from ..services.plan_suggestions import _sample_html
        body = ('<html><body>' + ' \n' * 18000
                + '<h1>  Book  </h1><p class="price">£51.77</p>'
                + ' \n' * 18000 + '</body></html>').encode('utf-8')
        sample = _sample_html(body, 'utf-8', max_bytes=256)
        self.assertIn('>  Book  <', sample)
        self.assertIn('class="price"', sample)
        self.assertIn('£51.77', sample)
        self.assertLessEqual(len(sample.encode('utf-8')), 256)
