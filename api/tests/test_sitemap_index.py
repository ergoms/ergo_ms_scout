"""Индекс — общая подготовка обхода, а не независимый сбор для каждой карты."""

import os
from datetime import timedelta
from threading import Thread
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from ..models import Run,Task
from ..services.tasks_exec import publish_ready,recover_expired_leases,run_one,_claim
from .fixtures.sitemap_index_catalog import SitemapIndexCatalog,index_plan,xml_index,xml_urls


class SitemapIndexTests(TestCase):
    def setUp(self):
        self.site = SitemapIndexCatalog()
        Thread(target=self.site.serve_forever,daemon=True).start()
        self.addCleanup(self.site.server_close)
        self.addCleanup(self.site.shutdown)
        self.enterContext(patch.dict(os.environ,{'SCOUT_TASK_RUNNER':'inline',
            'SCOUT_TRUSTED_FETCH_ENDPOINTS':f'127.0.0.1:{self.site.server_port}'}))
        self.user = get_user_model().objects.create_superuser(username='scout-sitemap-index')
        self.client=APIClient()
        self.client.force_authenticate(self.user)

    def prepare(self,path='/index.xml',**crawl):
        plan=index_plan(self.site.origin,path)
        plan['crawl'].update(crawl)
        response=self.client.post('/api/scout/jobs/',plan,format='json')
        self.assertEqual(response.status_code,201,response.data)
        job=response.data['public_id']
        run=self.client.post(f'/api/scout/jobs/{job}/explore/').data['public_id']
        publish_ready(run)
        endpoint=f'/api/scout/runs/{run}/'
        self.assertEqual(self.client.post(endpoint+'approve/').status_code,200)
        return run,endpoint

    def collect(self,path='/index.xml',**crawl):
        run,endpoint=self.prepare(path,**crawl)
        response=self.client.post(endpoint+'start/')
        self.assertEqual(response.status_code,202,response.data)
        publish_ready(run)
        return Run.objects.get(public_id=run)

    def test_index_nested_gzip_and_dedup_share_one_collection(self):
        run,endpoint=self.prepare()
        self.assertEqual(self.site.hits['/index.xml'],0)
        self.client.post(endpoint+'start/')
        publish_ready(run)
        result=self.client.get(endpoint,{'summary':1}).data
        self.assertEqual(result['status'],'completed',result)
        self.assertTrue(result['sitemap']['ready'])
        self.assertEqual(result['sitemap']['maps_processed'],4)
        self.assertEqual(result['sitemap']['maps_discovered'],4)
        stored=Run.objects.get(public_id=run)
        self.assertEqual(stored.records.count(),3)
        self.assertEqual(stored.tasks.filter(kind='sitemap').count(),4)
        for path in ('/index.xml','/nested.xml','/one.xml.gz','/two.xml','/products/k-01','/sale/k-01'):
            self.assertEqual(self.site.hits[path],1,path)
        self.assertFalse(stored.snapshots.filter(source_url__contains='.xml').exists())
        self.assertTrue(all(row.depth==0 for row in stored.crawl_urls.filter(state='done')))
        before=(stored.records.count(),sum(record.evidence.count() for record in stored.records.all()))
        for task in stored.tasks.filter(kind='sitemap'):
            self.assertFalse(run_one(str(task.public_id),task.fencing_token))
        self.assertEqual(before,(stored.records.count(),sum(record.evidence.count() for record in stored.records.all())))

    def test_cycle_fails_before_opening_html_frontier(self):
        run=self.collect('/cycle.xml')
        self.assertEqual(run.status,'failed')
        self.assertEqual(run.tasks.get(status='failed').error_code,'sitemap_cycle')
        self.assertFalse(run.crawl_urls.exists())
        self.assertEqual(self.site.hits['/cycle.xml'],1)

    def test_cycle_between_shared_children_is_detected(self):
        self.site.xml['/shared.xml']=xml_index([self.site.origin+'/left.xml',self.site.origin+'/right.xml'])
        self.site.xml['/left.xml']=xml_index([self.site.origin+'/right.xml'])
        self.site.xml['/right.xml']=xml_index([self.site.origin+'/left.xml'])
        run=self.collect('/shared.xml')
        self.assertEqual(run.status,'failed')
        self.assertEqual(run.tasks.get(status='failed').error_code,'sitemap_cycle')
        self.assertFalse(run.crawl_urls.exists())

    def test_bad_child_cancels_remaining_maps_without_partial_records(self):
        run=self.collect('/broken-index.xml')
        self.assertEqual(run.status,'failed')
        self.assertFalse(run.records.exists())
        self.assertFalse(run.crawl_urls.exists())
        self.assertEqual(run.tasks.get(status='failed').error_code,'sitemap_invalid')

    def test_host_scope_and_private_ports_are_not_requested(self):
        for child in ('https://example.org/map.xml', 'http://127.0.0.1:1/private.xml'):
            self.site.xml['/bad-scope.xml']=xml_index([child])
            run=self.collect('/bad-scope.xml')
            self.assertEqual(run.status,'failed')
            self.assertIn(run.tasks.get(status='failed').error_code,('sitemap_scope','url_blocked'))
            self.assertFalse(run.crawl_urls.exists())

    def test_redirect_alias_deduplicates_parsing_and_cycle_redirect_fails(self):
        self.site.xml['/alias.xml']=(self.site.origin+'/one.xml.gz',)
        self.site.xml['/aliases.xml']=xml_index([self.site.origin+'/one.xml.gz',self.site.origin+'/alias.xml'])
        run=self.collect('/aliases.xml')
        self.assertEqual(run.status,'completed')
        self.assertEqual(run.tasks.get(idempotency_key='sitemap').payload['sitemap_scan']['processed'],2)
        self.site.xml['/alias.xml']=(self.site.origin+'/aliases.xml',)
        run=self.collect('/aliases.xml')
        self.assertEqual(run.status,'failed')
        self.assertEqual(run.tasks.get(status='failed').error_code,'sitemap_cycle')

    def test_map_count_depth_and_total_byte_limits_fail_closed(self):
        with patch('modules.scout.api.services.sitemap_scan.MAX_MAPS',2):
            run=self.collect()
        self.assertEqual(run.status,'failed')
        self.assertEqual(run.tasks.get(status='failed').error_code,'sitemap_limit')
        with patch('modules.scout.api.services.sitemap_scan.MAX_DEPTH',1):
            run=self.collect()
        self.assertEqual(run.status,'failed')
        with patch('modules.scout.api.services.sitemap_scan.MAX_XML_BYTES',10):
            run=self.collect()
        self.assertEqual(run.status,'failed')
        self.assertFalse(run.records.exists())

    def test_global_page_limit_and_path_filters_apply_across_maps(self):
        run=self.collect(max_pages=2,visit=[{'kind':'prefix','pattern':'/products/'}])
        self.assertEqual(run.status,'completed')
        self.assertEqual(run.stop_reason,'max_pages')
        self.assertEqual(run.crawl_urls.filter(state='done').count(),2)
        self.assertFalse(run.snapshots.filter(source_url__contains='/sale/').exists())

    def test_gzipped_sitemap_still_rejects_more_than_one_million_unpacked_bytes(self):
        xml = xml_urls([self.site.origin + '/products/k-01'])
        self.site.xml['/large.xml.gz'] = xml.ljust(1_000_001)
        run = self.collect('/large.xml.gz')
        self.assertEqual(run.status, 'failed')
        task = run.tasks.get(idempotency_key='sitemap')
        self.assertEqual(task.error_code, 'too_large')
        self.assertEqual(task.payload['xml_allowance'], 1_000_000)
        self.assertFalse(run.crawl_urls.exists())

    def test_pending_scan_survives_lost_publication_and_lease_recovery(self):
        run_id,endpoint=self.prepare()
        with patch('modules.scout.api.views.schedule_publish'):
            self.client.post(endpoint+'start/')
        run=Run.objects.get(public_id=run_id)
        root=run.tasks.get(kind='sitemap')
        self.assertTrue(run_one(str(root.public_id),root.fencing_token))
        self.assertEqual(run.tasks.filter(kind='sitemap',status='pending').count(),2)
        self.assertFalse(run.crawl_urls.exists())
        child=run.tasks.filter(kind='sitemap',status='pending').first()
        claimed,token=_claim(str(child.public_id),child.fencing_token)
        Task.objects.filter(pk=claimed.pk).update(lease_until=timezone.now()-timedelta(seconds=1))
        recover_expired_leases()
        self.assertFalse(run_one(str(child.public_id),token))
        publish_ready(run_id)
        run.refresh_from_db()
        self.assertEqual(run.status,'completed')
        self.assertEqual(run.records.count(),3)

    def test_empty_and_malformed_index_entries_are_rejected(self):
        for body in ('<sitemapindex/>','<sitemapindex><sitemap><loc>/relative.xml</loc></sitemap></sitemapindex>',
                     '<!DOCTYPE sitemapindex><sitemapindex/>'):
            self.site.xml['/bad.xml']=body
            run=self.collect('/bad.xml')
            self.assertEqual(run.status,'failed')
            self.assertFalse(run.crawl_urls.exists())

    def test_pause_resume_and_cancel_keep_the_preparation_barrier(self):
        uid,endpoint=self.prepare()
        with patch('modules.scout.api.views.schedule_publish'):
            self.client.post(endpoint+'start/')
        run=Run.objects.get(public_id=uid)
        root=run.tasks.get(kind='sitemap')
        run_one(str(root.public_id),root.fencing_token)
        self.assertFalse(run.crawl_urls.exists())
        self.assertEqual(self.client.post(endpoint+'pause/').data['status'],'paused')
        child=run.tasks.filter(kind='sitemap',status='pending').first()
        self.assertFalse(run_one(str(child.public_id),child.fencing_token))
        self.client.post(endpoint+'resume/')
        publish_ready(uid)
        run.refresh_from_db()
        self.assertEqual(run.status,'completed')
        uid,endpoint=self.prepare()
        with patch('modules.scout.api.views.schedule_publish'):
            self.client.post(endpoint+'start/')
        run=Run.objects.get(public_id=uid)
        root=run.tasks.get(kind='sitemap')
        run_one(str(root.public_id),root.fencing_token)
        child=run.tasks.filter(kind='sitemap',status='pending').first()
        self.client.post(endpoint+'cancel/')
        self.assertFalse(run_one(str(child.public_id),child.fencing_token))
        self.assertFalse(run.crawl_urls.exists())
