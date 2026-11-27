import json
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings

from ..services.html_extract import extract_html
from ..services.plan_suggestions import SuggestionSelectorError, suggest_plan, _response_schema
from ..services.suggestion_stability import unstable_suggestion_path


@override_settings(SCOUT_AI_ENABLED=True)
class SuggestionStabilityTests(SimpleTestCase):
    body = b'<html><body><h1>Book A</h1><p class="price">51.77</p><p id="fallback">9.00</p></body></html>'

    def field(self, name='price', kind='css', selector='.price', scope='page'):
        return dict(name=name, kind=kind, selector=selector, scope=scope, many=False)

    def test_default_generation_grammar_uses_css_and_explicit_xpath_is_separate(self):
        for goal, expected in (('Title and price', ['css']), ('Use XPath for title', ['css', 'xpath'])):
            schema = _response_schema(goal)
            self.assertEqual(schema['properties']['fields']['items']['properties']['kind']['enum'], expected)
            self.assertEqual(schema['properties']['record']['anyOf'][1]['properties']['kind']['enum'], expected)
        self.assertEqual(_response_schema('Default again')['properties']['fields']['items']['properties']['kind']['enum'], ['css'])

    def proposal(self, fields, body=None, record=None):
        snapshot = SimpleNamespace(body=body or self.body, charset='utf-8',
                                   source_url='https://books.example/product',
                                   proxy_profile_revision_public_id=None)
        with patch('modules.scout.api.services.plan_suggestions._generate',
                   return_value=json.dumps({'record': record, 'fields': fields})) as model:
            result = suggest_plan(snapshot, 'Title and price as text')
        model.assert_called_once()
        return result

    def test_structural_rules_survive_changed_values_and_leave_source_untouched(self):
        fields = [self.field('title', selector='h1'), self.field()]
        draft = self.proposal(fields)
        changed = self.body.replace(b'Book A', b'Book B').replace(b'51.77', b'89.90')
        result = extract_html(changed, draft['fields'], draft['record'])
        self.assertEqual(result[0]['values']['title']['raw'], 'Book B')
        self.assertEqual(result[0]['values']['price']['raw'], '89.90')
        self.assertIn(b'Book A', self.body)
        self.assertIsNone(unstable_suggestion_path(self.body, fields, None))

    def test_observed_title_and_price_union_are_rejected_at_the_rule_path(self):
        for name, selector in (
            ('title', '//h1[contains(text(), "Book A")]'),
            ('price', '//p[contains(text(), "51.77")]|//td[contains(text(), "51.77")]'),
        ):
            with self.subTest(name=name), self.assertRaises(SuggestionSelectorError) as failure:
                self.proposal([self.field(name, 'xpath', selector)])
            self.assertEqual(failure.exception.code, 'selector')
            self.assertEqual(failure.exception.path, '/fields/0/selector')
            self.assertIn('зависит от текущего', str(failure.exception))

    def test_equal_match_count_cannot_hide_switching_to_a_different_node(self):
        selector = '//p[text()="51.77"] | //p[@id="fallback"][not(//p[text()="51.77"])]'
        with self.assertRaises(SuggestionSelectorError):
            self.proposal([self.field(kind='xpath', selector=selector)])

    def test_fixed_label_outside_the_value_is_allowed(self):
        body = b'<table><tr><th>Price</th><td>51.77</td></tr></table>'
        self.proposal([self.field(kind='xpath', selector='//tr[th[normalize-space(.)="Price"]]/td')], body)

    def test_record_relative_rules_and_value_dependent_container(self):
        body = b'<div><article><h2>Book A</h2><p class="price">51.77</p></article><article><h2>Book B</h2><p class="price">9.00</p></article></div>'
        fields = [self.field('title', selector='h2', scope='record'), self.field(scope='record')]
        self.proposal(fields, body, {'kind': 'css', 'selector': 'article'})
        with self.assertRaises(SuggestionSelectorError) as failure:
            self.proposal(fields, body, {'kind': 'xpath', 'selector': '//article[h2="Book A"]'})
        self.assertEqual(failure.exception.path, '/record/selector')
        fields[0] = self.field('title', 'xpath', './/h2[text()="Book A"]', 'record')
        with self.assertRaises(SuggestionSelectorError) as failure:
            self.proposal(fields, body, {'kind': 'css', 'selector': 'article'})
        self.assertEqual(failure.exception.path, '/fields/0/selector')

    def test_text_nodes_and_structural_scalar_formulas_remain_supported(self):
        fields = [self.field('title', 'xpath', '//h1/text()'),
                  self.field('count', 'xpath', 'count(//p)')]
        self.proposal(fields)
