from io import BytesIO
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase
from PIL import Image

from modules.scout.api.services import visual_assets
from modules.scout.api.services.errors import FetchError


_PAGE = 'https://catalog.example/item/123'


def _assets(*, revision=None, page=_PAGE):
    snapshot = SimpleNamespace(source_url=page, user_public_id='owner', proxy_profile_revision_public_id=revision)
    return visual_assets.VisualAssets(snapshot, page)


def _css(body, final_url, charset='UTF-8'):
    return {'body': body.encode(charset), 'content_type': f'text/css; charset={charset}', 'final_url': final_url}


def _png():
    output = BytesIO()
    Image.new('RGB', (2, 2), (1, 2, 3)).save(output, 'PNG')
    return {'body': output.getvalue(), 'content_type': 'image/png'}


class VisualAssetsTests(SimpleTestCase):
    def test_stylesheet_keeps_rules_in_a_large_minified_bundle(self):
        text = '.before{margin:0}' * 30000 + '.product{display:flex;gap:12px}'

        def fetch(url, *, max_bytes, **options):
            self.assertLess(len(text.encode('utf-8')), max_bytes)
            return _css(text, url)

        with patch.object(visual_assets, 'fetch_public_css', side_effect=fetch):
            assets = _assets()
            source, url = assets.stylesheet('/large.css')
            self.assertTrue(assets.style(source, url).endswith('.product{display:flex;gap:12px}'))

    def test_css_is_decoded_and_relative_import_uses_redirect_destination(self):
        main = _css('@import "common.css" screen; .title{content:"Мышь";font-size:20px}',
                    'https://cdn.example/assets/main.css', charset='windows-1251')
        common = _css('@import "main.css"; .title{color:red}', 'https://cdn.example/assets/common.css')
        with patch.object(visual_assets, 'fetch_public_css', side_effect=[main, common, main]) as fetch:
            assets = _assets()
            text, url = assets.stylesheet('/original.css')
            result = assets.style(text, url)
            self.assertEqual(assets.stylesheet('/original.css'), (text, url))

        self.assertIn('@media screen { .title{color:red}}', result)
        self.assertIn('content:"Мышь"', result)
        self.assertNotIn('@import', result)
        self.assertEqual(
            [call.args[0] for call in fetch.call_args_list],
            ['https://catalog.example/original.css', 'https://cdn.example/assets/common.css',
             'https://cdn.example/assets/main.css'],
        )

    def test_rewrites_urls_in_stylesheet_and_inline_attributes_from_same_cache(self):
        with patch.object(visual_assets, 'fetch_public_image', return_value=_png()) as fetch:
            assets = _assets()
            css = assets.style('.title{background:url("../photo.png"); mask:url(icon.svg)}')
            inline = assets.style('background:url(../photo.png); border:1px solid', declarations=True)

        self.assertIn('background:url("data:image/png;base64,', css)
        self.assertIn('mask:none', css)
        self.assertIn('background:url("data:image/png;base64,', inline)
        self.assertIn('border:1px solid', inline)
        self.assertEqual(fetch.call_count, 1)

    def test_platform_proxy_is_reused_and_private_resource_targets_are_rejected(self):
        with patch.object(visual_assets, 'fetch_proxy_image', return_value=_png()) as fetch:
            assets = _assets(revision='revision')
            self.assertIsNone(assets.image('http://127.0.0.1:8888/a.png'))
            self.assertIsNone(assets.image('http://service.localhost/a.png'))
            self.assertTrue(assets.image('https://cdn.example/a.png'))

        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(fetch.call_args.kwargs['revision_id'], 'revision')
        self.assertEqual(fetch.call_args.kwargs['owner'], 'owner')
        redirect_allowed = fetch.call_args.kwargs['url_allowed']
        self.assertFalse(redirect_allowed('https://[::1]/image.png'))
        self.assertFalse(redirect_allowed('https://192.168.2.1/image.png'))
        self.assertTrue(redirect_allowed('https://img.example/image.png'))

    def test_bad_responses_use_byte_budget_and_are_not_retried(self):
        with (
            patch.object(visual_assets, '_MAX_BYTES', 256),
            patch.object(
                visual_assets, 'fetch_public_image',
                side_effect=FetchError('too_large', 'Слишком большой файл', retryable=False),
            ) as fetch,
        ):
            assets = _assets()
            self.assertIsNone(assets.image('/a.png'))
            self.assertIsNone(assets.image('/a.png#fragment'))
            self.assertIsNone(assets.image('/b.png'))

        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(fetch.call_args.kwargs['max_bytes'], 256)

    def test_unrecognizable_raster_falls_back(self):
        with patch.object(visual_assets, 'fetch_public_image',
                          return_value={'body': b'<html>', 'content_type': 'image/png'}):
            self.assertIsNone(_assets().image('/a.png'))

    def test_animated_image_is_limited_across_all_frames(self):
        output = BytesIO()
        frames = [Image.new('RGB', (8, 8), (index, 0, 0)) for index in range(3)]
        frames[0].save(output, 'GIF', save_all=True, append_images=frames[1:])
        file = {'body': output.getvalue(), 'content_type': 'image/gif'}
        with patch.object(visual_assets, 'fetch_public_image', return_value=file):
            with patch.object(visual_assets, '_MAX_ANIMATION_PIXELS', 128):
                self.assertIsNone(_assets().image('/animation.gif'))
            self.assertTrue(_assets().image('/animation.gif').startswith('data:image/gif;base64,'))

    def test_limits_multiple_output_occurrences_of_a_cached_image(self):
        with (
            patch.object(visual_assets, '_MAX_CSS_IMAGES_CHARS', 130),
            patch.object(visual_assets, 'fetch_public_image', return_value=_png()) as fetch,
        ):
            result = _assets().style('.first{background:url(/a.png)}.second{background:url(/a.png)}')
        self.assertEqual(result.count('data:image/png;base64,'), 1)
        self.assertIn('.second{background:none}', result)
        self.assertEqual(fetch.call_count, 1)
