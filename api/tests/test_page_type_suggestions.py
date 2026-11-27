"""Несколько типов дают один проверяемый черновик без запуска сбора и расширения границ моделью."""

import copy
import json
import os
from datetime import timedelta
from threading import Thread
from unittest.mock import patch
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.test import TestCase,override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from ..models import VisualSnapshot,RetentionLease,Job
from ..services.tasks_exec import publish_ready
from .fixtures.spec002_catalog import CatalogServer


@override_settings(SCOUT_AI_ENABLED=True)
class PageTypeSuggestionTests(TestCase):
    def setUp(self):
        self.site=CatalogServer()
        Thread(target=self.site.serve_forever,daemon=True).start()
        self.addCleanup(self.site.server_close)
        self.addCleanup(self.site.shutdown)
        self.enterContext(patch.dict(os.environ,{'SCOUT_TASK_RUNNER':'inline',
            'SCOUT_TRUSTED_FETCH_ENDPOINTS':f'127.0.0.1:{self.site.server_port}'}))
        self.user=get_user_model().objects.create_superuser(username='scout-ai-page-types')
        self.client=APIClient()
        self.client.force_authenticate(self.user)
        self.samples=[]
        for path,kind,action in (('/catalog/','catalog','discover'),('/products/a','product','extract')):
            snapshot=VisualSnapshot.objects.create(user_public_id=self.user.public_id,status='ready',charset='utf-8',
                source_url=self.site.origin+path,final_url=self.site.origin+path,
                body=f'<html><body>{self.site.pages[path]}</body></html>'.encode())
            self.samples.append({'snapshot_public_id':str(snapshot.public_id),'id':kind,'action':action,
                                 'match':[{'kind':'prefix','pattern':'/catalog/' if action=='discover' else '/products/'}]})
        self.body={'goal':'Название и цена в рублях','pages':self.samples}
        self.rules={'record':None,'fields':[
            {'name':'title','kind':'css','selector':'h1','scope':'page','many':False},
            {'name':'price','kind':'css','selector':'.price','scope':'page','many':False,
             'conversion':{'type':'money','currency':'RUB','decimal_separator':','}},
        ],'page_types':[{'id':'product','selectors':{
            'title':{'kind':'css','selector':'h1'},'price':{'kind':'css','selector':'.price'},
        }}]}

    def post(self,body=None):
        return self.client.post('/api/scout/plan-suggestions/',body or self.body,format='json')

    def test_value_bound_override_is_rejected_with_snapshot_and_type(self):
        VisualSnapshot.objects.filter(public_id=self.samples[1]['snapshot_public_id']).update(
            body='<h1>Book A</h1><p class="price">1250,00 ₽</p>'.encode())
        reply = copy.deepcopy(self.rules)
        reply['page_types'][0]['selectors']['price'] = {
            'kind': 'xpath', 'selector': '//p[contains(text(), "1250,00")]',
        }
        with patch('modules.scout.api.services.plan_suggestions.bridge') as provider:
            provider.call.return_value = json.dumps(reply)
            response = self.post()
        self.assertEqual(response.status_code, 422, response.data)
        self.assertEqual(response.data['code'], 'selector')
        self.assertEqual(response.data['path'], '/crawl/page_types/1/selectors/price/selector')
        self.assertEqual(response.data['snapshot_public_id'], self.samples[1]['snapshot_public_id'])
        self.assertEqual(response.data['page_type_id'], 'product')
        self.assertIn('зависит от текущего', response.data['detail'])
        self.assertFalse(Job.objects.exists())

    def test_one_proposal_then_normal_preview_confirmation_and_collection_without_model(self):
        with patch('modules.scout.api.services.plan_suggestions.bridge') as provider:
            provider.call.return_value=json.dumps(self.rules)
            response=self.post()
            self.assertEqual(response.status_code,200,response.data)
            schema = provider.call.call_args.kwargs['format']
            self.assertEqual(schema['properties']['fields']['items']['properties']['kind']['enum'], ['css'])
            self.assertEqual(schema['properties']['page_types']['items']['properties']['id']['enum'], ['product'])
            self.assertEqual(Job.objects.count(),0)
            draft=response.data['draft']
            self.assertEqual(draft['crawl']['mode'],'site')
            self.assertEqual(draft['seed_urls'],[self.site.origin+'/catalog/'])
            self.assertEqual(draft['crawl']['page_types'][0]['action'],'discover')
            self.assertEqual(set(draft['crawl']['page_types'][1]['selectors']),{'title','price'})
            prompt=json.loads(provider.call.call_args.kwargs['messages'][1]['content'])
            self.assertEqual([row['id'] for row in prompt['pages']],['catalog','product'])
            job=self.client.post('/api/scout/jobs/',draft,format='json')
            self.assertEqual(job.status_code,201,job.data)
            pending=self.client.post(f"/api/scout/jobs/{job.data['public_id']}/explore/")
            self.assertEqual(pending.status_code,202,pending.data)
            uid=pending.data['public_id']
            publish_ready(uid)
            path=f'/api/scout/runs/{uid}/'
            preview=self.client.get(path,{'value_format':'typed_v1'}).data
            self.assertTrue(preview['page_type_preview']['can_approve'])
            self.assertEqual(self.client.post(path+'start/').status_code,409)
            self.assertEqual(self.client.post(path+'approve/').status_code,200)
            self.client.post(path+'start/')
            publish_ready(uid)
            self.assertEqual(self.client.get(path).data['status'],'completed')
            rows=self.client.get(path+'records/',{'value_format':'typed_v1'}).data['records']
            self.assertEqual(len(rows),2)
            self.assertTrue(all(row['evidence'] for row in rows))
            provider.call.assert_called_once()
        self.assertEqual(RetentionLease.objects.count(),0)

    def test_wrong_selector_identifies_snapshot_type_and_rule(self):
        with patch('modules.scout.api.services.plan_suggestions.bridge') as provider:
            for selector,code in (('.not-present','selector_no_match'),('h1 >','selector')):
                reply=copy.deepcopy(self.rules)
                reply['page_types'][0]['selectors']['price']['selector']=selector
                provider.call.return_value=json.dumps(reply)
                response=self.post()
                self.assertEqual(response.status_code,422,response.data)
                self.assertEqual(response.data['code'],code)
                self.assertEqual(response.data['snapshot_public_id'],self.samples[1]['snapshot_public_id'])
                self.assertEqual(response.data['page_type_id'],'product')
                self.assertEqual(response.data['path'],'/crawl/page_types/1/selectors/price/selector')
            for target,path in (('field','/fields/0/selector'),('record','/record/selector')):
                reply=copy.deepcopy(self.rules)
                if target=='field':
                    reply['fields'][0]['selector']=''
                else:
                    reply['record']={'kind':'css','selector':''}
                provider.call.return_value=json.dumps(reply)
                response=self.post()
                self.assertEqual(response.status_code,422,response.data)
                self.assertEqual(response.data['path'],path)
                self.assertEqual(response.data['snapshot_public_id'],self.samples[1]['snapshot_public_id'])
                self.assertEqual(response.data['page_type_id'],'product')

    def test_foreign_expired_and_unready_snapshots_do_not_call_model(self):
        second=VisualSnapshot.objects.get(public_id=self.samples[1]['snapshot_public_id'])
        with patch('modules.scout.api.services.plan_suggestions.bridge') as provider:
            VisualSnapshot.objects.filter(pk=second.pk).update(user_public_id=uuid4())
            self.assertEqual(self.post().status_code,404)
            VisualSnapshot.objects.filter(pk=second.pk).update(user_public_id=self.user.public_id,
                created_at=timezone.now()-timedelta(hours=2))
            self.assertEqual(self.post().status_code,404)
            VisualSnapshot.objects.filter(pk=second.pk).update(created_at=timezone.now(),status='fetching')
            self.assertEqual(self.post().status_code,409)
            provider.call.assert_not_called()

    def test_type_match_conflict_and_proxy_mismatch_are_rejected_before_model(self):
        with patch('modules.scout.api.services.plan_suggestions.bridge') as provider:
            invalid=copy.deepcopy(self.body)
            invalid['pages'][1]['match']=[{'kind':'prefix','pattern':'/'}]
            self.assertEqual(self.post(invalid).status_code,409)
            VisualSnapshot.objects.filter(public_id=self.samples[1]['snapshot_public_id']).update(
                proxy_profile_revision_public_id=uuid4())
            response=self.post()
            self.assertEqual(response.status_code,409,response.data)
            self.assertEqual(response.data['code'],'snapshot_proxy_mismatch')
            provider.call.assert_not_called()

    def test_model_cannot_change_type_ids_bounds_or_add_missing_schema_fields(self):
        with patch('modules.scout.api.services.plan_suggestions.bridge') as provider:
            variants=[]
            extra=copy.deepcopy(self.rules)
            extra['page_types'][0]['match']=[{'kind':'prefix','pattern':'/'}]
            variants.append(extra)
            missing=copy.deepcopy(self.rules)
            del missing['page_types'][0]['selectors']['price']
            variants.append(missing)
            renamed=copy.deepcopy(self.rules)
            renamed['page_types'][0]['id']='another'
            variants.append(renamed)
            for reply in variants:
                provider.call.return_value=json.dumps(reply)
                self.assertEqual(self.post().status_code,502)
            provider.call.return_value=None
            self.assertEqual(self.post().status_code,503)

    def test_request_shape_limits_and_sanitization(self):
        snapshot=VisualSnapshot.objects.get(public_id=self.samples[1]['snapshot_public_id'])
        snapshot.body=bytes(snapshot.body)+b'<script>private-script</script><form>private-form</form>'
        snapshot.save(update_fields=['body'])
        with patch('modules.scout.api.services.plan_suggestions.bridge') as provider:
            provider.call.return_value=json.dumps(self.rules)
            self.assertEqual(self.post().status_code,200)
            prompt=provider.call.call_args.kwargs['messages'][1]['content']
            self.assertNotIn('private-script',prompt)
            self.assertNotIn('private-form',prompt)
            for pages in (self.samples[:1],self.samples*3,[self.samples[0],self.samples[0]]):
                body={'goal':self.body['goal'],'pages':pages}
                self.assertEqual(self.post(body).status_code,400)

    def test_second_extracting_type_uses_its_own_selectors(self):
        self.site.pages['/archive/c']='<h2>Другой тип</h2><span class="cost">90,00</span>'
        snapshot=VisualSnapshot.objects.create(user_public_id=self.user.public_id,status='ready',charset='utf-8',
            source_url=self.site.origin+'/archive/c',body=self.site.pages['/archive/c'].encode())
        body=copy.deepcopy(self.body)
        body['pages'].append({'snapshot_public_id':str(snapshot.public_id),'id':'archive','action':'extract',
                             'match':[{'kind':'prefix','pattern':'/archive/'}]})
        reply=copy.deepcopy(self.rules)
        reply['page_types'].append({'id':'archive','selectors':{
            'title':{'kind':'css','selector':'h2'},'price':{'kind':'css','selector':'.cost'},
        }})
        with patch('modules.scout.api.services.plan_suggestions.bridge') as provider:
            provider.call.return_value=json.dumps(reply)
            response=self.post(body)
            self.assertEqual(response.status_code,200,response.data)
            self.assertEqual(response.data['draft']['crawl']['page_types'][2]['selectors']['price']['selector'],'.cost')

    def test_equivalent_field_repeated_by_model_is_coalesced_but_type_conflict_is_rejected(self):
        reply=copy.deepcopy(self.rules)
        duplicate=copy.deepcopy(reply['fields'][1])
        duplicate['selector']='.another-price'
        reply['fields'].append(duplicate)
        with patch('modules.scout.api.services.plan_suggestions.bridge') as provider:
            provider.call.return_value=json.dumps(reply)
            response=self.post()
            self.assertEqual(response.status_code,200,response.data)
            self.assertEqual(len(response.data['draft']['fields']),2)
            reply['fields'][2]['conversion']['currency']='USD'
            provider.call.return_value=json.dumps(reply)
            self.assertEqual(self.post().status_code,502)
