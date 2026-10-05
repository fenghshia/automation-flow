import io
import json
import logging
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch
from uuid import uuid4

from flask import Flask
from sqlalchemy import event

from video_filter.tests.support import DatabaseTestCase, app, db, signature, bundle_arguments
from dashboard import register_dashboard, register_service
from env import EnvConfig
from video_filter.dashboard import register_dashboard as register_video_dashboard
from video_filter.feature_store import FeatureStore
from video_filter.models import Location, ModelRun, ScanRun, Task
from video_filter.models.records import utc_now
from video_filter.observability import WorkerLogHandler
from video_filter.progress import ProgressHandler, extra, read_progress, track_task
from video_filter.reporting import dashboard_details, status_snapshot
from video_filter.worker_client import _run_worker


class ShellTests(unittest.TestCase):
    def test_registration_navigation_and_future_service(self):
        application = Flask(__name__)
        application.add_url_rule('/example/', 'example', lambda: 'example')
        arguments = dict(key='example', title='示例服务', description='本地测试', endpoint='example')
        register_service(application, **arguments)
        register_service(application, **arguments)
        register_dashboard(application)
        client = application.test_client()
        self.assertEqual('/dashboard/', client.get('/').location)
        page = client.get('/dashboard/').get_data(as_text=True)
        self.assertIn('示例服务', page)
        self.assertIn('/example/', page)
        for filename in ('dashboard.js', 'dashboard.css', 'favicon.svg'):
            with client.get('/dashboard/static/' + filename) as response:
                self.assertEqual(200, response.status_code)
        with self.assertRaises(ValueError):
            register_service(application, **{**arguments, 'title': 'Conflict'})

    def test_existing_home_route_is_preserved(self):
        application = Flask(__name__)
        application.add_url_rule('/', 'existing', lambda: 'original-home')
        register_dashboard(application)
        self.assertEqual(b'original-home', application.test_client().get('/').data)


class DashboardTests(DatabaseTestCase):
    def setUp(self):
        super().setUp()
        register_video_dashboard(app)
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        manifest = self.root / 'manifest.json'
        manifest.write_text(json.dumps({'specification': signature().to_dict()}), encoding='utf-8')
        self.settings = {'enabled': True, 'classifier': 'mil', 'model_manifest': manifest,
                         'state_directory': self.root / 'state', 'transfer_enabled': True,
                         'directories': {'confirmed_like': self.root}}
        self.client = app.test_client()

    def tearDown(self):
        self.temporary.cleanup()
        super().tearDown()

    def get(self, fragment=False):
        with patch.object(EnvConfig, 'video_filter_settings', return_value=self.settings):
            return self.client.get('/video_filter/dashboard/' + ('fragment' if fragment else ''))

    def fixture(self):
        asset, variant, config = self.identities()
        store = FeatureStore()
        store.save(db.session, store.prepare(**bundle_arguments(asset.id, variant.id)))
        db.session.add(Location(variant_id=variant.id, role='confirmed_like',
            path=str(self.root / 'synthetic.mp4'), size_bytes=10, modified_ns=20))
        task = Task(kind='extract', status='running', dedup_key='e' * 64,
            variant_id=variant.id, config_revision_id=config.id, claimed_at=utc_now() - timedelta(seconds=30),
            input_snapshot={'path': str(self.root / '<img onerror=alert(1)>.mp4')})
        db.session.add_all([task, ScanRun(config_revision_id=config.id, complete=True, finished_at=utc_now())])
        for kind, slot in (('logistic_regression', 'active'), ('mil', 'mil')):
            db.session.add(ModelRun(model_type=kind, active_slot=slot, status='active',
                feature_signature=signature().digest, dataset_snapshot={}, validation={'balanced_accuracy': .8},
                threshold=.5, model_blob=b'private-numerical-payload', sha256='a' * 64))
        db.session.commit()
        handler = ProgressHandler(self.settings['state_directory'], task.id)
        handler.write({'stage': '窗口提取', 'modality': 'dino', 'completed': 3, 'total': 10})
        handler.close()
        return task

    def test_disabled_and_invalid_configuration_do_not_touch_database(self):
        with patch.object(db.session, 'execute', side_effect=AssertionError('DB accessed')):
            self.settings['enabled'] = False
            self.assertIn('尚未开启', self.get().get_data(as_text=True))
            with patch.object(EnvConfig, 'video_filter_settings', side_effect=RuntimeError('private-config-marker')):
                response = self.client.get('/video_filter/dashboard/')
            page = response.get_data(as_text=True)
            self.assertIn('配置暂不可用', page)
            self.assertNotIn('private-config-marker', page)

    def test_missing_schema_returns_safe_refreshable_page(self):
        Task.__table__.drop(db.engine)
        response = self.get(fragment=True)
        self.assertEqual(200, response.status_code)
        page = response.get_data(as_text=True)
        self.assertIn('数据库数据暂不可用', page)
        self.assertNotIn('SELECT ', page)
        self.assertEqual('no-store', response.headers['Cache-Control'])

    def test_enabled_rendering_escaped_names_real_phase_progress_and_read_only(self):
        task = self.fixture()
        queries = []
        def capture(connection, cursor, statement, parameters, context, executemany):
            queries.append(statement)
        event.listen(db.engine, 'before_cursor_execute', capture)
        try:
            with patch.object(db.session, 'commit', side_effect=AssertionError('Unexpected commit')):
                response = self.get()
                fragment = self.get(fragment=True)
        finally:
            event.remove(db.engine, 'before_cursor_execute', capture)
        page = response.get_data(as_text=True)
        self.assertEqual(200, response.status_code)
        self.assertEqual(200, fragment.status_code)
        for text in ('30.0%', '当前阶段窗口 3 / 10', '100.0%', '80.0%', '暂无共同用户反馈',
                     '注意力 MIL', '逻辑回归', '百分比表示当前阶段', '&lt;', task.id):
            self.assertIn(text, page)
        self.assertNotIn('<img onerror=alert(1)>', page)
        self.assertNotIn(str(self.root), page)
        self.assertNotIn('private-numerical-payload', page)
        self.assertTrue(all(query.lstrip().upper().startswith('SELECT') for query in queries))
        for query in queries:
            if 'LIMIT ? OFFSET ?' not in query:  # zero-row compatibility schema probe
                self.assertNotIn('arrays_blob', query)
            self.assertNotIn('model_blob', query)
        self.assertEqual('no-store', response.headers['Cache-Control'])
        self.assertEqual(405, self.client.post('/video_filter/dashboard/fragment').status_code)

    def test_finished_task_ignores_cached_progress_and_zero_total_is_unknown(self):
        task = self.fixture()
        handler = ProgressHandler(self.settings['state_directory'], task.id)
        handler.write({'stage': '模型训练', 'epoch': 2, 'epochs': 0})
        self.assertIsNone(dashboard_details(db.session, self.settings)['current_task']['progress']['percent'])
        self.assertEqual(200, self.get(fragment=True).status_code)  # loss can be absent
        task.status, task.finished_at = 'succeeded', utc_now()
        db.session.commit()
        details = dashboard_details(db.session, self.settings)
        self.assertIsNone(details['current_task'])
        self.assertIsNone(details['tasks'][0]['progress'])
        handler.close()

    def test_signature_changes_exclude_old_summaries(self):
        self.fixture()
        self.settings['model_manifest'] = None
        snapshot = status_snapshot(db.session, self.settings)
        self.assertEqual(1, snapshot['counts']['present_variants'])
        self.assertEqual(0, snapshot['counts']['summarized_present_variants'])
        self.assertEqual(0, snapshot['counts']['trainable_positive_assets'])
        self.assertFalse(snapshot['automatic_processing'])


class ProgressTests(unittest.TestCase):
    def test_metadata_whitelist_corruption_and_canonical_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            identifier = str(uuid4())
            handler = ProgressHandler(directory, identifier)
            handler.write({'stage': 'DINO', 'completed': 2, 'total': 4, 'path': 'private-path',
                           'training_loss': float('nan'), 'epoch': 'bad-number'})
            value = read_progress(directory, identifier)
            self.assertEqual(2, value['completed'])
            for key in ('path', 'training_loss', 'epoch'):
                self.assertNotIn(key, value)
            self.assertFalse(handler.path.with_suffix('.pending').exists())
            self.assertIsNone(read_progress(directory, '../invalid-id'))
            handler.path.write_text('{invalid-json', encoding='utf-8')
            self.assertIsNone(read_progress(directory, identifier))
            for field in ({'epoch': 'bad'}, {'total': float('inf')}, {'stage': []}, {'path': 'private'}):
                handler.path.write_text(json.dumps({**value, **field}), encoding='utf-8')
                self.assertIsNone(read_progress(directory, identifier))
            handler.close()

    def test_worker_protocol_flows_to_live_cache_and_handler_is_removed(self):
        logger = logging.getLogger('video_filter')
        old_level = logger.level
        logger.setLevel(logging.INFO)
        try:
            with tempfile.TemporaryDirectory() as directory:
                identifier = str(uuid4())
                output = io.StringIO()
                protocol = WorkerLogHandler(output, identifier)
                record = logging.LogRecord('video_filter.extraction', logging.INFO, '', 1, 'progress', (), None)
                record.video_filter_progress = extra(identifier, '音频提取', completed=5, total=10)['video_filter_progress']
                protocol.emit(record)
                process = MagicMock()
                process.__enter__.return_value = process
                process.stdout = io.StringIO(output.getvalue())
                process.wait.return_value = process.poll.return_value = 0
                before = list(logger.handlers)
                with track_task(directory, identifier):
                    with patch('video_filter.worker_client.subprocess.Popen', return_value=process), patch('video_filter.worker_client.ProcessTree'):
                        _run_worker({'task_id': identifier, 'state_directory': directory}, 1,
                                    module='test-module', prefix='test', load=lambda _: None)
                    self.assertEqual(5, read_progress(directory, identifier)['completed'])
                self.assertEqual(before, logger.handlers)
                protocol.close()
        finally:
            logger.setLevel(old_level)

    def test_progress_io_failure_does_not_abort_business_work(self):
        with patch('video_filter.progress.ProgressHandler', side_effect=OSError('test I/O failure')):
            with track_task('unused-test-path', str(uuid4())) as handler:
                self.assertIsNone(handler)
