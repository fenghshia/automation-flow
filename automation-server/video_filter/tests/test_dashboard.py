import io
import json
import logging
import os
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch
from uuid import uuid4

from flask import Flask
from sqlalchemy import event, select

from video_filter.tests.support import DatabaseTestCase, app, db, signature, bundle_arguments
from dashboard import register_dashboard, register_service
from env import EnvConfig
from video_filter.dashboard import register_dashboard as register_video_dashboard
from video_filter.feature_store import FeatureStore
from video_filter.models import Asset, Location, ModelRun, ScanRun, Task, Variant
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
        from video_filter.apis.control import blueprint
        if blueprint.name not in app.blueprints:
            app.register_blueprint(blueprint)
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

    def get_training(self, fragment=False, group=None):
        with patch.object(EnvConfig, 'video_filter_settings', return_value=self.settings):
            return self.client.get('/video_filter/dashboard/training' + ('/fragment' if fragment else ''),
                                   query_string={'group': group} if group else {})

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
                     '注意力 MIL', '逻辑回归', '百分比表示当前阶段',
                           '自动模型训练已关闭，仅手动训练', '&lt;', task.id):
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

    def test_legacy_metrics_without_labels_render_on_both_pages(self):
        from video_filter.evaluation import actual_metrics
        self.fixture()
        metrics = actual_metrics(db.session)
        for kind in ("logistic_regression", "mil"):
            metrics['models'][kind].pop('labels')
            metrics['paired'][kind].pop('labels')
        metrics['versions'] = [{'model_id': model.id, 'accuracy': None, 'reviewed_assets': 0}
                               for model in db.session.scalars(select(ModelRun))]
        with patch('video_filter.reporting.actual_metrics', return_value=metrics):
            for response in (self.get(fragment=True), self.get_training(fragment=True)):
                self.assertEqual(200, response.status_code)
                page = response.get_data(as_text=True)
                self.assertIn('不喜欢', page)
                self.assertNotIn('UndefinedError', page)
                self.assertIn('—', page)

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

    def test_training_page_shows_current_and_unaccepted_results_without_payloads(self):
        task = self.fixture()
        db.session.add(ModelRun(model_type='mil', status='validated', feature_signature=signature().digest, dataset_snapshot=[],
            validation={'balanced_accuracy': .55, 'roc_auc': .58, 'training_assets': 14, 'validation_assets': 6},
            threshold=.4, model_blob=b'private-latest-payload', sha256='a' * 64))
        db.session.commit()
        queries = []
        def capture(connection, cursor, statement, *args):
            queries.append(statement)
        event.listen(db.engine, 'before_cursor_execute', capture)
        try:
            with patch.object(db.session, 'commit', side_effect=AssertionError('Unexpected commit')):
                response = self.get_training()
                fragment = self.get_training(fragment=True)
        finally:
            event.remove(db.engine, 'before_cursor_execute', capture)
        page = response.get_data(as_text=True)
        self.assertEqual(200, response.status_code)
        self.assertEqual(200, fragment.status_code)
        for text in ('模型训练', '验证集平衡准确率', '80.0%', '55.0%', '0.580',
                     '验证未达标', '手动训练 逻辑回归', '手动训练 注意力 MIL', '训练集准确率未记录',
                     '/video_filter/train', '/video_filter/dashboard/training/fragment'):
            self.assertIn(text, page)
        for text in (str(self.root), 'private-latest-payload', 'private-numerical-payload', task.id):
            self.assertNotIn(text, page)
        self.assertTrue(all(query.lstrip().upper().startswith('SELECT') for query in queries))
        self.assertTrue(all('model_blob' not in query for query in queries))
        self.assertEqual('no-store', response.headers['Cache-Control'])
        self.assertEqual(405, self.client.post('/video_filter/dashboard/training').status_code)
        with self.client.get('/video_filter/dashboard/static/training.js') as response:
            self.assertEqual(200, response.status_code)

    def test_training_button_requires_samples_and_tracks_pending_training_progress(self):
        task = self.fixture()
        self.assertIn('data-ready="false"', self.get_training().get_data(as_text=True))
        for label, amount in ((1, 9), (0, 10)):
            for _ in range(amount):
                asset = Asset(label=label)
                db.session.add(asset)
                db.session.flush()
                variant = Variant(asset_id=asset.id, sha256=uuid4().hex * 2, size_bytes=10)
                db.session.add(variant)
                db.session.commit()
                store = FeatureStore()
                arguments = bundle_arguments(asset.id, variant.id)
                arguments['source_sha256'] = variant.sha256
                store.save(db.session, store.prepare(**arguments))
        page = self.get_training().get_data(as_text=True)
        self.assertEqual(2, page.count('data-ready="true"'))
        training = Task(kind='train', status='running', dedup_key='d' * 64, config_revision_id=task.config_revision_id,
            claimed_at=utc_now(), input_snapshot={'model_type': 'mil'})
        db.session.add(training)
        db.session.commit()
        handler = ProgressHandler(self.settings['state_directory'], training.id)
        handler.write({'stage': 'MIL训练', 'epoch': 3, 'epochs': 60, 'validation_loss': .12345})
        handler.close()
        page = self.get_training(fragment=True).get_data(as_text=True)
        self.assertEqual(1, page.count('data-ready="true"'))
        for text in ('训练已排队或进行中', '第 3 / 60 轮', '0.12345', training.id):
            self.assertIn(text, page)

    def test_active_model_update_notice_is_visible_on_both_pages(self):
        self.fixture()
        model = db.session.scalar(select(ModelRun).where(ModelRun.model_type == 'logistic_regression'))
        model.validation = {**model.validation, 'needs_update': True, 'update_reason': 'training_labels_changed'}
        db.session.commit()
        for response in (self.get(fragment=True), self.get_training(fragment=True)):
            page = response.get_data(as_text=True)
            self.assertEqual(200, response.status_code)
            self.assertIn('模型需要更新', page)
            self.assertIn('当前版本继续预测', page)
            self.assertIn('等新版本通过验收后替换', page)
        self.assertEqual('active', model.status)

    def test_training_empty_disabled_and_schema_error_states(self):
        self.assertIn('暂无训练完成的模型版本', self.get_training().get_data(as_text=True))
        self.settings['enabled'] = False
        with patch.object(db.session, 'execute', side_effect=AssertionError('DB accessed')):
            self.assertIn('尚未开启', self.get_training().get_data(as_text=True))
        self.settings['enabled'] = True
        Task.__table__.drop(db.engine)
        page = self.get_training(fragment=True).get_data(as_text=True)
        self.assertIn('数据库数据暂不可用', page)
        self.assertNotIn('SELECT ', page)

    def test_group_training_links_and_models_remain_scoped(self):
        from video_filter.scope import group_scope, current_scope
        groups = [{**self.settings, 'grouped': True, 'name': name} for name in ('alpha', 'beta')]
        for group in groups:
            with group_scope(db.session, group):
                db.session.add(ModelRun(model_type='mil', status='active', active_slot='mil',
                    feature_signature=signature().digest, dataset_snapshot=[], validation={'balanced_accuracy': .91 if group['name'] == 'alpha' else .72},
                    threshold=.5, sha256='a' * 64, model_blob=b'fixture-model'))
                db.session.commit()
        self.settings = {**self.settings, 'grouped': True, 'groups': groups}
        page = self.get_training().get_data(as_text=True)
        self.assertIn('请选择要训练的分组', page)
        self.assertNotIn('class="training-form"', page)
        page = self.get_training(group='alpha').get_data(as_text=True)
        self.assertIn('91.0%', page)
        self.assertNotIn('72.0%', page)
        self.assertIn('/video_filter/groups/alpha/train', page)
        self.assertIn('/training/fragment?group=alpha', page)
        def settings(**kwargs):
            scope = current_scope()
            return scope['settings'] if scope else self.settings
        result = type('TrainingTask', (), {'id': str(uuid4()), 'status': 'queued'})()
        with patch.object(EnvConfig, 'video_filter_settings', side_effect=settings), \
                patch('video_filter.runtime.enqueue', return_value=result) as submit:
            response = self.client.post('/video_filter/groups/alpha/train', json={'model_type': 'mil',
                'acceptance': {'like_precision': .75, 'dislike_precision': .9}})
        self.assertEqual(202, response.status_code)
        self.assertEqual('alpha', submit.call_args.args[1]['name'])
        self.assertEqual('mil', submit.call_args.kwargs['model_type'])
        self.assertEqual({'like_precision': .75, 'dislike_precision': .9}, submit.call_args.kwargs['acceptance'])

    def test_manual_training_endpoint_enqueues_and_returns_safe_failure(self):
        result = type('TrainingTask', (), {'id': str(uuid4()), 'status': 'queued'})()
        with patch.object(EnvConfig, 'video_filter_settings', return_value=self.settings), \
                patch('video_filter.runtime.enqueue', return_value=result) as submit:
            response = self.client.post('/video_filter/train', json={'model_type': 'logistic_regression'})
        self.assertEqual(202, response.status_code)
        self.assertEqual(result.id, response.json['task_id'])
        self.assertEqual('logistic_regression', submit.call_args.kwargs['model_type'])
        with patch.object(EnvConfig, 'video_filter_settings', return_value=self.settings), \
                patch('video_filter.runtime.enqueue', side_effect=ValueError('insufficient_confirmed_samples_minimum_10_per_class')):
            response = self.client.post('/video_filter/train', json={'model_type': 'mil'})
        self.assertEqual(409, response.status_code)
        self.assertEqual('insufficient_confirmed_samples_minimum_10_per_class', response.json['error_code'])

    def test_manual_training_accepts_separate_gates_and_rejects_invalid_input(self):
        result = type('TrainingTask', (), {'id': str(uuid4()), 'status': 'queued'})()
        gates = {'like_precision': .75, 'dislike_precision': .9}
        with patch.object(EnvConfig, 'video_filter_settings', return_value=self.settings), \
                patch('video_filter.runtime.enqueue', return_value=result) as submit:
            response = self.client.post('/video_filter/train', json={'model_type': 'mil', 'acceptance': gates})
            self.assertEqual(202, response.status_code)
            self.assertEqual(gates, submit.call_args.kwargs['acceptance'])
            submit.reset_mock()
            for value in (None, {}, {'like_precision': True, 'dislike_precision': .9},
                          {'like_precision': .8, 'dislike_precision': 90}):
                response = self.client.post('/video_filter/train', json={'acceptance': value})
                self.assertEqual(400, response.status_code)
                self.assertEqual('invalid_training_acceptance', response.json['error_code'])
            submit.assert_not_called()
        page = self.get_training().get_data(as_text=True)
        for text in ('喜欢预测精确率至少', '不喜欢预测精确率至少', '召回率', '仅手动训练', 'value="80.0"'):
            self.assertIn(text, page)

    def test_training_results_show_saved_gates_and_both_label_counts(self):
        from video_filter.evaluation import acceptance_result, classification_metrics
        self.fixture()
        model = db.session.scalar(select(ModelRun).where(ModelRun.model_type == 'logistic_regression'))
        metrics = classification_metrics([0, 0, 1, 1], [0, 1, 1, 1])
        result = acceptance_result(metrics, {'like_precision': .6, 'dislike_precision': .9})
        model.validation = {**model.validation, 'labels': metrics['labels'], 'acceptance': result}
        db.session.commit()
        page = self.get_training().get_data(as_text=True)
        for text in ('66.7%', '100.0%', '≥ 60.0%', '≥ 90.0%', '真实样本', '预测正确', '预测错误'):
            self.assertIn(text, page)
        self.assertFalse(status_snapshot(db.session, self.settings)['automatic_training'])


class ProgressTests(unittest.TestCase):
    def test_windows_replace_denial_retries_then_publishes_and_cleans_staging(self):
        with tempfile.TemporaryDirectory() as directory:
            identifier = str(uuid4())
            handler = ProgressHandler(directory, identifier)
            replace = os.replace
            blocked = PermissionError("temporary reader lock")
            blocked.winerror = 5
            attempts = []
            def publish(source, destination):
                attempts.append(source)
                if len(attempts) <= 2:
                    raise blocked
                replace(source, destination)
            with patch("video_filter.progress.os.replace", side_effect=publish), \
                    patch("video_filter.progress.time.sleep") as pause:
                handler.write({"stage": "DINO", "completed": 3, "total": 10})
            self.assertEqual(3, len(attempts))
            self.assertEqual(2, pause.call_count)
            self.assertEqual(3, read_progress(directory, identifier)["completed"])
            self.assertEqual([], list(handler.path.parent.glob("*.pending")))
            handler.close()

    def test_persistent_progress_denial_keeps_previous_snapshot_and_reports_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            identifier = str(uuid4())
            handler = ProgressHandler(directory, identifier)
            previous = read_progress(directory, identifier)
            for windows in (True, False):
                with self.subTest(windows=windows):
                    blocked = PermissionError("persistent denial")
                    if windows:
                        blocked.winerror = 32
                    record = logging.LogRecord("video_filter.extraction", logging.INFO, "", 1, "progress", (), None)
                    record.video_filter_progress = extra(identifier, "DINO", completed=3)["video_filter_progress"]
                    with patch("video_filter.progress.os.replace", side_effect=blocked) as replace, \
                            patch("video_filter.progress.time.sleep"), \
                            patch("video_filter.observability.log_failure") as failures:
                        handler.emit(record)
                    self.assertEqual(5 if windows else 1, replace.call_count)
                    failures.assert_called_once()
                    self.assertEqual(previous, read_progress(directory, identifier))
                    self.assertEqual([], list(handler.path.parent.glob("*.pending")))
            handler.close()

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
