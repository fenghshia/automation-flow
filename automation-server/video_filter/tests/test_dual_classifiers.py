import hashlib
import tempfile
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import numpy as np
from sqlalchemy import select

from video_filter.tests.support import DatabaseTestCase, app, db, signature
from video_filter.tests import test_workflow as workflow
from video_filter.feature_store import FeatureStore
from video_filter.evaluation import actual_metrics
from video_filter.feedback import FeedbackJournal, feedback_values
from video_filter.features.contract import canonical_json
from video_filter.learning import dataset, score, train
from video_filter.mil import ARCHITECTURE, WIDTH, deserialize, probability, serialize
from video_filter.model_registry import active_models
from video_filter.models import Asset, FeatureBundle, Location, ModelRun, Prediction, PredictionOutcome, Task, Variant
from video_filter.models.records import utc_now
from video_filter.prediction import predict_both
from video_filter.runtime import enqueue, enqueue_automatic, execute
from video_filter.transfer import classify


class DualClassifierTests(DatabaseTestCase):
    save_bundle = workflow.WorkflowTests.save_bundle
    trained_model = workflow.WorkflowTests.trained_model
    scan = workflow.WorkflowTests.scan
    register_file = workflow.WorkflowTests.register_file

    def setUp(self):
        super().setUp()
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        roles = ("confirmed_like", "predicted_like", "predicted_dislike", "unclassified", "compressed_like")
        self.settings = {"enabled": True, "directories": {role: root / role for role in roles},
            "device": "cpu", "worker_cpu_threads": 1,
            "state_directory": root / "state", "stable_seconds": 1, "missing_seconds": 2,
            "task_timeout_seconds": 30, "scan_interval_seconds": 1, "classifier": "logistic_regression",
            "deletion_feedback_enabled": True, "transfer_enabled": True,
            "lineage_enabled": False, "model_manifest": root / "manifest.json", "ffmpeg_directory": root / "tools"}
        for path in self.settings["directories"].values():
            path.mkdir()
        self.now = utc_now()

    def tearDown(self):
        self.temporary.cleanup()
        super().tearDown()

    def constant_model(self, kind, positive, snapshot=None):
        if kind == "mil":
            shapes = {"encoder.0.weight": (128, WIDTH * 2), "encoder.0.bias": (128,),
                "attention_v.weight": (64, 128), "attention_v.bias": (64,),
                "attention_u.weight": (64, 128), "attention_u.bias": (64,),
                "attention_w.weight": (1, 64), "attention_w.bias": (1,),
                "classifier.weight": (1, 128), "classifier.bias": (1,)}
            state = {name: np.zeros(shape, dtype=np.float32).tolist() for name, shape in shapes.items()}
            state["classifier.bias"] = [4 if positive else -4]
            parameters = {"schema": 2, "model_type": "mil", "architecture": ARCHITECTURE,
                "mean": [0] * WIDTH, "scale": [1] * WIDTH,
                "state": state}
        else:
            dimensions = WIDTH * 3
            parameters = {"schema": 1, "mean": [0] * dimensions, "scale": [1] * dimensions,
                          "coef": [0] * dimensions, "intercept": 4 if positive else -4}
        payload = serialize(parameters) if kind == "mil" else canonical_json(parameters).encode()
        run = ModelRun(model_type=kind, feature_signature=signature().digest, dataset_snapshot=snapshot or [],
            validation={"fixture": True}, threshold=0.5, model_blob=payload,
            sha256=hashlib.sha256(payload).hexdigest(), status="active", active_slot="mil" if kind == "mil" else "active")
        db.session.add(run)
        db.session.commit()
        return run

    def predict_fixture(self):
        path, asset, variant = self.register_file("unclassified")
        bundle = self.save_bundle(asset, variant)
        lr = self.constant_model("logistic_regression", True)
        mil = self.constant_model("mil", False)
        with patch("video_filter.runtime.load_model_manifest", return_value=(signature(), {})):
            task = enqueue(db.session, self.settings, "predict", variant.id)
        predictions = predict_both(db.session, task, settings=self.settings)
        return path, asset, variant, bundle, lr, mil, task, predictions

    def feedback(self, asset, label):
        return FeedbackJournal(self.settings["state_directory"]).submit(db.session,
            feedback_values(asset, label, {"reason": "user_confirmed"}))

    def test_classify_reuses_complete_predictions_without_worker_or_extra_outcomes(self):
        import shutil
        from video_filter.supervisor import resource_mode
        path, asset, variant, _, _, _, predicted_task, original = self.predict_fixture()
        predicted_task.status = "succeeded"
        db.session.commit()
        with patch("video_filter.runtime.load_model_manifest", return_value=(signature(), {})):
            task = enqueue(db.session, self.settings, "classify", variant.id)
        self.assertEqual({kind: row.id for kind, row in original.items()}, task.input_snapshot["prediction_ids"])
        self.assertIsNone(resource_mode("classify", task.input_snapshot))
        released = []
        copy = shutil.copyfileobj
        def unlocked_copy(*args, **kwargs):
            self.assertFalse(db.session().in_transaction())
            self.assertEqual([True], released)
            return copy(*args, **kwargs)
        self.settings["release_gpu"] = lambda: released.append(True)
        with patch("video_filter.transfer.predict_both", side_effect=AssertionError("Prediction must be reused")), \
                patch("video_filter.transfer.shutil.copyfileobj", side_effect=unlocked_copy):
            operation = classify(db.session, self.settings, task)
        self.assertEqual("source_cleaned", operation.status)
        self.assertEqual(2, db.session.query(Prediction).count())
        self.feedback(asset, 1)
        self.assertEqual(2, db.session.query(PredictionOutcome).count())
        self.assertEqual(1, actual_metrics(db.session)["paired"]["reviewed_assets"])

    def test_inference_runs_without_transaction_and_rejects_mid_inference_feedback(self):
        path, asset, variant = self.register_file("unclassified")
        self.save_bundle(asset, variant)
        self.constant_model("logistic_regression", True)
        with patch("video_filter.runtime.load_model_manifest", return_value=(signature(), {})):
            task = enqueue(db.session, self.settings, "predict", variant.id)
        def changed(session, *args, **kwargs):
            self.assertFalse(session().in_transaction())
            self.feedback(asset, 0)
            return .8
        with patch("video_filter.prediction.score", side_effect=changed):
            with self.assertRaisesRegex(ValueError, "asset_feedback_changed"):
                predict_both(db.session, task, settings=self.settings)
        db.session.rollback()
        self.assertEqual(0, db.session.query(Prediction).count())

    def test_parameter_change_during_inference_rejects_frozen_output(self):
        path, asset, variant = self.register_file("unclassified")
        self.save_bundle(asset, variant)
        model = self.constant_model("logistic_regression", True)
        with patch("video_filter.runtime.load_model_manifest", return_value=(signature(), {})):
            task = enqueue(db.session, self.settings, "predict", variant.id)
        identifier = model.id
        def changed(session, *args, **kwargs):
            self.assertFalse(session().in_transaction())
            row = session.get(ModelRun, identifier)
            row.model_blob += b" "  # Valid JSON with a different, valid checksum.
            row.sha256 = hashlib.sha256(row.model_blob).hexdigest()
            session.commit()
            return .8
        with patch("video_filter.prediction.score", side_effect=changed):
            with self.assertRaisesRegex(ValueError, "model_parameters_changed"):
                predict_both(db.session, task, settings=self.settings)
        db.session.rollback()
        self.assertEqual(0, db.session.query(Prediction).count())

    def test_summary_change_during_inference_rejects_frozen_output(self):
        path, asset, variant = self.register_file("unclassified")
        bundle = self.save_bundle(asset, variant)
        self.constant_model("logistic_regression", True)
        with patch("video_filter.runtime.load_model_manifest", return_value=(signature(), {})):
            task = enqueue(db.session, self.settings, "predict", variant.id)
        def changed(session, *args, **kwargs):
            session.get(FeatureBundle, bundle.bundle_id).status = "failed"
            session.commit()
            return .8
        with patch("video_filter.prediction.score", side_effect=changed):
            with self.assertRaisesRegex(ValueError, "summary_version_changed"):
                predict_both(db.session, task, settings=self.settings)
        db.session.rollback()
        self.assertEqual(0, db.session.query(Prediction).count())

    def test_grouped_waiting_prediction_does_not_block_new_extraction(self):
        from video_filter.scope import TEST_GROUP, TEST_EPOCH
        self.settings.update(grouped=True, name="isolated_fixture", dataset_group_id=TEST_GROUP,
            reset_epoch=TEST_EPOCH, extract_concurrency=6)
        path, asset, variant = self.register_file("unclassified")
        self.save_bundle(asset, variant)
        self.constant_model("logistic_regression", True)
        pending = path.with_name("pending.mp4")
        pending.write_bytes(b"synthetic pending video")
        self.scan(3)
        self.scan(5)
        with patch("video_filter.runtime.load_model_manifest", return_value=(signature(), {})):
            prediction = enqueue(db.session, self.settings, "predict", variant.id)
            self.assertEqual("queued", prediction.status)
            enqueue_automatic(db.session, self.settings)
        self.assertEqual(1, db.session.query(Task).filter_by(kind="predict", status="queued").count())
        self.assertEqual(1, db.session.query(Task).filter_by(kind="extract", status="queued").count())

    def test_two_predictions_persist_idempotently_without_moving_or_labelling(self):
        path, asset, _, _, _, _, task, predictions = self.predict_fixture()
        repeated = predict_both(db.session, task, settings=self.settings)
        self.assertEqual({kind: value.id for kind, value in predictions.items()}, {kind: value.id for kind, value in repeated.items()})
        self.assertEqual(2, db.session.query(Prediction).count())
        self.assertTrue(path.exists())
        self.assertIsNone(asset.label)
        self.assertTrue(all(row.evaluation_eligible for row in predictions.values()))
        self.assertEqual(0, actual_metrics(db.session)["paired"]["reviewed_assets"])

    def test_actual_feedback_metrics_pairing_replay_and_preference_changes(self):
        _, asset, _, _, lr, mil, _, _ = self.predict_fixture()
        event = self.feedback(asset, 1)
        metrics = actual_metrics(db.session)
        self.assertEqual(1, metrics["paired"]["reviewed_assets"])
        self.assertEqual(1.0, metrics["models"]["logistic_regression"]["accuracy"])
        self.assertEqual(0.0, metrics["models"]["mil"]["accuracy"])
        self.assertEqual(2, db.session.query(PredictionOutcome).count())
        # Feedback on a previously unseen sample does not retire either model.
        self.assertEqual("active", lr.status)
        self.assertEqual("active", mil.status)
        FeedbackJournal(self.settings["state_directory"]).submit(db.session, {
            "asset_id": event.asset_id, "label": event.label, "expected_revision": event.expected_revision,
            "event_key": event.event_key, "evidence": event.evidence})
        self.assertEqual(2, db.session.query(PredictionOutcome).count())
        self.feedback(asset, 0)
        metrics = actual_metrics(db.session)
        self.assertEqual(4, db.session.query(PredictionOutcome).count())
        self.assertEqual(1, metrics["models"]["mil"]["reviewed_assets"])
        self.assertEqual(1.0, metrics["models"]["mil"]["accuracy"])

    def test_env_selects_mil_move_and_both_predictions_survive_user_deletion(self):
        self.settings["classifier"] = "mil"
        path, asset, variant, _, _, _, _, _ = self.predict_fixture()
        with patch("video_filter.runtime.load_model_manifest", return_value=(signature(), {})):
            task = enqueue(db.session, self.settings, "classify", variant.id)
        operation = classify(db.session, self.settings, task)
        self.assertEqual(self.settings["directories"]["predicted_dislike"].resolve(), Path(operation.destination_path).parent)
        self.assertFalse(path.exists())
        self.assertIsNone(asset.label)
        self.scan(5)
        Path(operation.destination_path).unlink()
        self.scan(6)
        self.scan(9)
        self.assertEqual(0, asset.label)
        metrics = actual_metrics(db.session)
        self.assertEqual(1, metrics["paired"]["reviewed_assets"])
        self.assertEqual(1, metrics["paired"]["mil"]["accuracy"])
        self.assertEqual(0, metrics["paired"]["logistic_regression"]["accuracy"])

    def test_move_to_confirmed_like_records_both_outcomes(self):
        path, asset, _, _, _, _, _, _ = self.predict_fixture()
        path.rename(self.settings["directories"]["confirmed_like"] / path.name)
        self.scan(3)
        self.scan(5)
        self.assertEqual(1, asset.label)
        self.assertEqual(1.0, actual_metrics(db.session)["paired"]["logistic_regression"]["accuracy"])

    def test_known_or_dataset_samples_never_count_as_actual_accuracy(self):
        path, asset, variant = self.register_file("confirmed_like")
        self.save_bundle(asset, variant)
        self.constant_model("logistic_regression", True)
        with patch("video_filter.runtime.load_model_manifest", return_value=(signature(), {})):
            task = enqueue(db.session, self.settings, "predict", variant.id)
        predictions = predict_both(db.session, task)
        self.assertFalse(predictions["logistic_regression"].evaluation_eligible)
        self.feedback(asset, 0)
        self.assertEqual(1, db.session.query(PredictionOutcome).count())
        self.assertEqual(0, actual_metrics(db.session)["models"]["logistic_regression"]["reviewed_assets"])

    def test_selected_unavailable_model_does_not_fall_back(self):
        self.settings["classifier"] = "mil"
        _, asset, variant = self.register_file("unclassified")
        self.save_bundle(asset, variant)
        self.constant_model("logistic_regression", True)
        with patch("video_filter.runtime.load_model_manifest", return_value=(signature(), {})):
            with self.assertRaisesRegex(ValueError, "selected_classifier_unavailable"):
                enqueue(db.session, self.settings, "classify", variant.id)
            task = enqueue(db.session, self.settings, "predict", variant.id)
        self.assertEqual({"logistic_regression"}, set(predict_both(db.session, task, "mil")))

    def test_training_mil_uses_existing_bags_and_preserves_lr_slot(self):
        lr = self.trained_model()
        settings = {**self.settings, "device": "cpu", "mil_epochs": 3, "mil_patience": 2, "mil_max_train_windows": 4}
        mil = train(db.session, signature().digest, model_type="mil", settings=settings)
        self.assertEqual("active", lr.status)
        self.assertEqual("mil", mil.model_type)
        self.assertEqual(3, mil.validation["epochs_completed"])
        bags, labels, snapshot = dataset(db.session, signature().digest, "mil")
        self.assertEqual(20, len(bags))
        self.assertEqual((2, WIDTH), bags[0][0].shape)
        self.assertEqual(lr.dataset_snapshot, snapshot)
        bundle = FeatureStore().require_ready(db.session, snapshot[0]["bundle_id"])
        if mil.status == "active":
            self.assertTrue(0 <= score(db.session, mil, bundle, settings=settings) <= 1)
            self.assertEqual(2, len(active_models(db.session)))
        # Neither re-extraction nor permanent transport files are required.
        self.assertEqual(20, db.session.query(FeatureBundle).count())
        self.assertFalse(list(settings["state_directory"].glob("training-*")))

    def test_chunked_pooling_matches_whole_bag_and_missing_audio(self):
        rng = np.random.default_rng(3)
        x = rng.normal(size=(23, WIDTH)).astype(np.float32)
        valid = np.ones_like(x, dtype=bool)
        valid[:, 768:] = False
        x[:, 768:] = 0
        parameters = deserialize(self.constant_model("mil", True).model_blob)
        for name, values in parameters["state"].items():
            parameters["state"][name] = rng.normal(scale=0.03, size=np.asarray(values).shape).tolist()
        self.assertAlmostEqual(probability(parameters, (x, valid), chunk_size=23),
                               probability(parameters, (x, valid), chunk_size=4), places=6)

    def test_torch_worker_and_numpy_inference_agree_with_variable_bags(self):
        from video_filter.worker_client import run_mil_training
        rng = np.random.default_rng(19)
        bags = []
        for index in range(20):
            x = rng.normal(size=(index % 8 + 1, WIDTH)).astype(np.float32)
            valid = np.ones_like(x, dtype=bool)
            if index % 3 == 0:
                valid[:, 768:] = False
            else:
                valid[:, -88:] = rng.random((len(x), 88)) > 0.3
            x[~valid] = 0
            bags.append((x, valid))
        fit, holdout = np.arange(14), np.arange(14, 20)
        result = run_mil_training(bags, np.arange(20) % 2, fit, holdout,
            {**self.settings, "device": "cpu", "mil_epochs": 1, "mil_max_train_windows": 3})
        for index, expected in zip(holdout, result["probabilities"]):
            self.assertAlmostEqual(expected, probability(result["parameters"], bags[index], chunk_size=2), places=5)

    def test_automatic_shadow_prediction_works_with_transfers_disabled(self):
        self.settings["transfer_enabled"] = False
        path, asset, variant = self.register_file("unclassified")
        self.save_bundle(asset, variant)
        self.constant_model("logistic_regression", True)
        with patch("video_filter.runtime.load_model_manifest", return_value=(signature(), {})), \
                patch("video_filter.runtime.enqueue", wraps=enqueue) as submit:
            enqueue_automatic(db.session, self.settings)
            self.assertEqual("predict", submit.call_args.args[2])
            task = db.session.execute(select(Task)).scalar_one()
            execute(db.session, self.settings, task)
        self.assertTrue(path.exists())
        self.assertEqual(1, db.session.query(Prediction).count())

    def test_ready_summary_is_predicted_before_other_files_are_extracted(self):
        from types import SimpleNamespace
        self.settings["transfer_enabled"] = False
        _, asset, ready = self.register_file("unclassified")
        self.save_bundle(asset, ready)
        self.constant_model("logistic_regression", True)
        pending = Variant(asset_id=asset.id, sha256="f" * 64, size_bytes=10)
        db.session.add(pending)
        db.session.flush()
        db.session.add(Location(variant_id=pending.id, role="unclassified", path="example-pending.mp4",
            current_path_key="f" * 64, size_bytes=10, modified_ns=1))
        db.session.commit()
        with patch("video_filter.runtime.load_model_manifest", return_value=(signature(), {})), \
                patch("video_filter.runtime.enqueue", return_value=SimpleNamespace(status="queued")) as submit:
            enqueue_automatic(db.session, self.settings)
        self.assertEqual("predict", submit.call_args.args[2])
        self.assertEqual(ready.id, submit.call_args.args[3])

    def test_metrics_api_separates_real_results_from_validation(self):
        from video_filter import register_video_filter
        register_video_filter()
        _, asset, _, _, _, _, _, _ = self.predict_fixture()
        self.feedback(asset, 1)
        with patch("env.EnvConfig.video_filter_settings", return_value=self.settings):
            response = app.test_client().get("/video_filter/metrics")
        self.assertEqual(200, response.status_code)
        self.assertEqual(1, response.json["paired"]["reviewed_assets"])
        self.assertNotIn("model_blob", response.get_data(as_text=True))

    def test_feedback_marks_only_affected_family_and_keeps_both_serving(self):
        _, asset, _ = self.register_file("confirmed_like")
        lr = self.constant_model("logistic_regression", True, [{"asset_id": asset.id,
            "label": 1, "label_revision": asset.label_revision}])
        mil = self.constant_model("mil", True)
        self.feedback(asset, 0)
        self.assertEqual("active", lr.status)
        self.assertEqual("active", lr.active_slot)
        self.assertTrue(lr.validation["needs_update"])
        self.assertEqual("active", mil.status)
        self.assertFalse(mil.validation.get("needs_update", False))
        _, target, variant = self.register_file("unclassified")
        self.save_bundle(target, variant)
        with patch("video_filter.runtime.load_model_manifest", return_value=(signature(), {})):
            task = enqueue(db.session, self.settings, "predict", variant.id)
        predictions = predict_both(db.session, task, settings=self.settings)
        self.assertEqual({lr.id, mil.id}, {row.model_id for row in predictions.values()})

    def test_same_label_feedback_records_history_without_model_update(self):
        from video_filter.learning import require_current
        from video_filter.models import FeedbackEvent
        _, asset, _ = self.register_file("confirmed_like")
        snapshot = [{"asset_id": asset.id, "label": 1, "label_revision": asset.label_revision}]
        model = self.constant_model("logistic_regression", True, snapshot)
        before = db.session.query(FeedbackEvent).count()
        revision = asset.label_revision
        for _ in range(2):
            self.feedback(asset, 1)
        self.assertEqual(before + 2, db.session.query(FeedbackEvent).count())
        self.assertEqual(revision + 2, asset.label_revision)
        self.assertEqual("active", model.status)
        self.assertFalse(model.validation.get("needs_update", False))
        require_current(db.session, snapshot)

    def test_flip_back_and_unrecorded_revisions_still_reject_training_snapshot(self):
        from video_filter.learning import require_current, training_labels_changed
        _, asset, _ = self.register_file("confirmed_like")
        snapshot = [{"asset_id": asset.id, "label": 1, "label_revision": asset.label_revision}]
        self.feedback(asset, 0)
        self.feedback(asset, 1)
        current = [{"asset_id": asset.id, "label": 1, "label_revision": asset.label_revision}]
        self.assertTrue(training_labels_changed(db.session, snapshot, current))
        with self.assertRaisesRegex(ValueError, "training_labels_changed"):
            require_current(db.session, snapshot)
        asset.label_revision += 1
        db.session.commit()
        with self.assertRaisesRegex(ValueError, "training_labels_changed"):
            require_current(db.session, current)

    def test_prediction_locks_target_and_both_datasets_before_model_foreign_keys(self):
        from sqlalchemy import event
        from sqlalchemy.dialects import postgresql
        path, target, variant = self.register_file("unclassified")
        self.save_bundle(target, variant)
        training = [Asset(label=1) for _ in range(3)]
        db.session.add_all(training)
        db.session.commit()
        def rows(assets):
            return [{"asset_id": item.id, "label": item.label, "label_revision": item.label_revision} for item in assets]
        self.constant_model("logistic_regression", True, rows(training[:2]))
        self.constant_model("mil", False, rows(training[1:]))
        with patch("video_filter.runtime.load_model_manifest", return_value=(signature(), {})):
            task = enqueue(db.session, self.settings, "predict", variant.id)
        locks = []
        def capture(state):
            if getattr(state.statement, "_for_update_arg", None) is not None:
                locks.append(state.statement)
        session = db.session()
        event.listen(session, "do_orm_execute", capture)
        try:
            with patch("video_filter.prediction.score", return_value=.8):
                predict_both(db.session, task, settings=self.settings)
        finally:
            event.remove(session, "do_orm_execute", capture)
        first = locks[0]
        expected_ids = {target.id, *(item.id for item in training)}
        self.assertIs(Asset, first.column_descriptions[0]["entity"])
        self.assertEqual(expected_ids, set(first.compile().params["id_1"]))
        self.assertIn("ORDER BY video_filter_asset.id", str(first.compile(dialect=postgresql.dialect())))
        self.assertIn("FOR UPDATE OF video_filter_asset", str(first.compile(dialect=postgresql.dialect())))
        self.assertIs(ModelRun, locks[1].column_descriptions[0]["entity"])
        self.assertIn("ORDER BY video_filter_model_run.id", str(locks[1].compile(dialect=postgresql.dialect())))
        self.assertIn("FOR SHARE OF video_filter_model_run", str(locks[1].compile(dialect=postgresql.dialect())))
        for statement in locks[2:]:
            if statement.column_descriptions[0]["entity"] is Asset:
                self.assertTrue(set(statement.compile().params["id_1"]) <= expected_ids,
                    "A model lock must never precede a new asset lock.")
        self.assertEqual(2, db.session.query(Prediction).count())
        self.assertTrue(path.exists())

    def test_feedback_on_unseen_asset_does_not_lock_unaffected_models(self):
        from sqlalchemy import event
        _, asset, _ = self.register_file("unclassified")
        lr = self.constant_model("logistic_regression", True)
        mil = self.constant_model("mil", False)
        locks = []
        def capture(state):
            if getattr(state.statement, "_for_update_arg", None) is not None:
                locks.append(state.statement)
        session = db.session()
        event.listen(session, "do_orm_execute", capture)
        try:
            self.feedback(asset, 0)
        finally:
            event.remove(session, "do_orm_execute", capture)
        self.assertTrue(locks)
        self.assertTrue(all(statement.column_descriptions[0]["entity"] is not ModelRun for statement in locks))
        self.assertEqual(("active", "active"), (lr.status, mil.status))

    def test_lr_retraining_does_not_retire_mil(self):
        mil = self.constant_model("mil", True)
        lr = self.trained_model()
        replacement = train(db.session, signature().digest)
        self.assertEqual("retired", lr.status)
        self.assertEqual("active", replacement.status)
        self.assertEqual("active", mil.status)

    def add_training_sample(self, label):
        from uuid import uuid4
        asset = Asset(label=label)
        db.session.add(asset)
        db.session.flush()
        variant = Variant(asset_id=asset.id, sha256=hashlib.sha256(uuid4().bytes).hexdigest(), size_bytes=10)
        db.session.add(variant)
        db.session.commit()
        self.save_bundle(asset, variant, float(label) * 2 - 1)
        return asset.id

    def test_new_samples_during_fit_do_not_discard_lr_or_mil_training(self):
        from sklearn.linear_model import LogisticRegression
        from video_filter.learning import training_snapshot
        self.trained_model()
        parameters = deserialize(self.constant_model("mil", True).model_blob)
        original_fit = LogisticRegression.fit
        added = []
        def fit(classifier, x, y):
            self.assertFalse(db.session().in_transaction())
            self.assertEqual(0, db.engine.pool.checkedout())
            result = original_fit(classifier, x, y)
            added.append(self.add_training_sample(1))
            return result
        def mil_fit(bags, y, fit, holdout, settings, task_id):
            self.assertFalse(db.session().in_transaction())
            self.assertEqual(0, db.engine.pool.checkedout())
            added.append(self.add_training_sample(0))
            return {"parameters": parameters, "probabilities": [probability(parameters, bags[i]) for i in holdout],
                    "measurements": {"epochs_completed": 1}}
        for kind in ("logistic_regression", "mil"):
            before = training_snapshot(db.session, signature().digest)
            with patch.object(LogisticRegression, "fit", new=fit), \
                    patch("video_filter.worker_client.run_mil_training", side_effect=mil_fit):
                run = train(db.session, signature().digest, model_type=kind, settings=self.settings)
            self.assertEqual(before, run.dataset_snapshot)
            self.assertNotIn(added[-1], {item["asset_id"] for item in run.dataset_snapshot})
            self.assertEqual(len(before) + 1, len(training_snapshot(db.session, signature().digest)))
            self.assertIn(run.status, ("active", "validated"))

    def test_changed_labels_or_missing_training_summary_still_reject_snapshot(self):
        from video_filter.learning import require_training_snapshot
        self.trained_model()
        _, _, rows = dataset(db.session, signature().digest)
        asset = db.session.get(Asset, rows[0]["asset_id"])
        asset.label, asset.label_revision = 1 - asset.label, asset.label_revision + 1
        db.session.commit()
        with self.assertRaisesRegex(ValueError, "training_labels_changed"):
            require_training_snapshot(db.session, signature().digest, rows)
        db.session.rollback()
        _, _, current = dataset(db.session, signature().digest)
        bundle = db.session.get(FeatureBundle, current[0]["bundle_id"])
        bundle.status = "failed"
        db.session.commit()
        with self.assertRaisesRegex(ValueError, "training_dataset_changed"):
            require_training_snapshot(db.session, signature().digest, current)

    def test_same_label_during_fit_can_publish_but_changed_label_cannot(self):
        from sklearn.linear_model import LogisticRegression
        old = self.trained_model()
        asset = db.session.get(Asset, old.dataset_snapshot[0]["asset_id"])
        original_fit = LogisticRegression.fit
        def fit_same(classifier, x, y):
            result = original_fit(classifier, x, y)
            self.feedback(asset, asset.label)
            return result
        with patch.object(LogisticRegression, "fit", new=fit_same):
            replacement = train(db.session, signature().digest)
        self.assertEqual("active", replacement.status)
        self.assertEqual("retired", old.status)
        def fit_changed(classifier, x, y):
            result = original_fit(classifier, x, y)
            self.feedback(asset, 1 - asset.label)
            return result
        with patch.object(LogisticRegression, "fit", new=fit_changed):
            with self.assertRaisesRegex(ValueError, "training_labels_changed"):
                train(db.session, signature().digest)
        db.session.rollback()
        self.assertEqual("active", replacement.status)
        self.assertTrue(replacement.validation["needs_update"])

    def test_unaccepted_retraining_preserves_old_active_version(self):
        old = self.trained_model()
        asset = db.session.get(Asset, old.dataset_snapshot[0]["asset_id"])
        opposite = db.session.get(Asset, next(item["asset_id"] for item in old.dataset_snapshot if item["label"] != asset.label))
        self.feedback(asset, 1 - asset.label)
        self.feedback(opposite, 1 - opposite.label)
        with patch("sklearn.linear_model.LogisticRegression.predict_proba", return_value=np.tile([.5, .5], (6, 1))):
            candidate = train(db.session, signature().digest)
        self.assertEqual("validated", candidate.status)
        self.assertFalse(candidate.validation["acceptance"]["passed"])
        self.assertIsNone(candidate.active_slot)
        self.assertEqual("active", old.status)
        self.assertEqual("active", old.active_slot)
        self.assertTrue(old.validation["needs_update"])

    def test_deleted_automatic_candidate_waits_for_scan_without_error_or_labelling(self):
        path, asset, variant = self.register_file("unclassified")
        self.save_bundle(asset, variant)
        self.constant_model("logistic_regression", True)
        path.unlink()
        with patch("video_filter.runtime.load_model_manifest", return_value=(signature(), {})), \
                patch("video_filter.runtime.log_exception") as failures:
            enqueue_automatic(db.session, self.settings)
        failures.assert_not_called()
        self.assertEqual(0, db.session.query(Task).count())
        self.assertIsNone(db.session.get(Asset, asset.id).label)
        self.assertEqual(1, db.session.query(FeatureBundle).count())

    def test_queued_prediction_for_retired_model_cancels_then_uses_new_model(self):
        from video_filter.runtime import process_round
        path, asset, variant = self.register_file("unclassified")
        self.save_bundle(asset, variant)
        old = self.constant_model("logistic_regression", True)
        with patch("video_filter.runtime.load_model_manifest", return_value=(signature(), {})):
            task = enqueue(db.session, self.settings, "predict", variant.id)
        old.status, old.active_slot = "retired", None
        db.session.commit()
        new = self.constant_model("logistic_regression", False)
        with patch("video_filter.runtime.load_model_manifest", return_value=(signature(), {})), \
                patch("video_filter.tracking.reconcile"), patch("video_filter.runtime.log_exception") as failures:
            process_round(db.session, self.settings)
            self.assertEqual("cancelled", db.session.get(Task, task.id).status)
            self.assertEqual("active_compatible_model_required", db.session.get(Task, task.id).error_code)
            self.assertEqual(0, db.session.query(Prediction).count())
            enqueue_automatic(db.session, self.settings)
            replacement = db.session.scalar(select(Task).where(Task.status == "queued"))
            self.assertNotEqual(task.id, replacement.id)
            self.assertEqual(new.id, replacement.input_snapshot["model_ids"]["logistic_regression"])
            execute(db.session, self.settings, replacement)
        failures.assert_not_called()
        self.assertEqual(new.id, db.session.scalar(select(Prediction.model_id)))
        self.assertTrue(path.exists())

    def test_prediction_after_user_deletion_is_not_recorded(self):
        path, asset, variant = self.register_file("unclassified")
        self.save_bundle(asset, variant)
        self.constant_model("logistic_regression", True)
        with patch("video_filter.runtime.load_model_manifest", return_value=(signature(), {})):
            task = enqueue(db.session, self.settings, "predict", variant.id)
        path.unlink()
        with self.assertRaises(OSError):
            predict_both(db.session, task)
        db.session.rollback()
        self.assertEqual(0, db.session.query(Prediction).count())

    def test_shadow_model_failure_does_not_publish_or_commit_partial_predictions(self):
        _, asset, variant = self.register_file("unclassified")
        self.save_bundle(asset, variant)
        self.constant_model("logistic_regression", True)
        mil = self.constant_model("mil", False)
        mil.model_blob = b"corrupt"
        db.session.commit()
        with patch("video_filter.runtime.load_model_manifest", return_value=(signature(), {})):
            task = enqueue(db.session, self.settings, "classify", variant.id)
        with self.assertRaisesRegex(ValueError, "model_checksum_mismatch"):
            classify(db.session, self.settings, task)
        db.session.rollback()
        self.assertEqual(0, db.session.query(Prediction).count())
        self.assertFalse(list(self.settings["directories"]["predicted_like"].iterdir()))

    def test_completed_first_twenty_candidates_do_not_starve_later_predictions(self):
        from types import SimpleNamespace
        self.settings["transfer_enabled"] = False
        _, asset, variant = self.register_file("unclassified")
        self.save_bundle(asset, variant)
        lr = self.constant_model("logistic_regression", True)
        variants = [variant]
        for index in range(1, 22):
            another = Variant(asset_id=asset.id, sha256=f"{index:064x}", size_bytes=10)
            db.session.add(another)
            db.session.flush()
            db.session.add(Location(variant_id=another.id, role="predicted_like", path=f"example-{index}.mp4",
                current_path_key=f"{index:064x}", size_bytes=10, modified_ns=1,
                created_at=utc_now() + timedelta(seconds=index)))
            db.session.commit()
            self.save_bundle(asset, another)
            variants.append(another)
        bundles = {row.variant_id: row for row in db.session.execute(select(FeatureBundle)).scalars()}
        for item in variants[:21]:
            db.session.add(Prediction(variant_id=item.id, bundle_id=bundles[item.id].id, model_id=lr.id,
                group_id=item.id, label_revision=0, score=0.9, threshold=0.5, predicted_label=1))
        db.session.commit()
        with patch("video_filter.runtime.load_model_manifest", return_value=(signature(), {})), \
                patch("video_filter.runtime.enqueue", return_value=SimpleNamespace(status="queued")) as submit:
            enqueue_automatic(db.session, self.settings)
        self.assertEqual("predict", submit.call_args.args[2])
        self.assertEqual(variants[-1].id, submit.call_args.args[3])

    def test_gpu_worker_failure_or_source_deletion_does_not_commit_partial_predictions(self):
        path, asset, variant = self.register_file("unclassified")
        self.save_bundle(asset, variant)
        self.constant_model("logistic_regression", True)
        self.constant_model("mil", False)
        with patch("video_filter.runtime.load_model_manifest", return_value=(signature(), {})):
            task = enqueue(db.session, self.settings, "classify", variant.id)
        with patch("video_filter.worker_client.run_mil_prediction", side_effect=ValueError("prediction_worker_failed")):
            with self.assertRaisesRegex(ValueError, "prediction_worker_failed"):
                classify(db.session, self.settings, task)
        db.session.rollback()
        self.assertEqual(0, db.session.query(Prediction).count())
        self.assertTrue(path.exists())
        self.assertFalse(list(self.settings["directories"]["predicted_like"].iterdir()))
        def deleted_during_inference(*args, **kwargs):
            path.unlink()
            return .8
        with patch("video_filter.worker_client.run_mil_prediction", side_effect=deleted_during_inference):
            with self.assertRaises(OSError):
                predict_both(db.session, task, settings=self.settings)
        db.session.rollback()
        self.assertEqual(0, db.session.query(Prediction).count())

    def test_transfer_gate_reuses_gpu_prediction_after_source_cleanup(self):
        from video_filter.transfer import check_gate
        from video_filter.worker_client import run_mil_prediction
        self.settings["classifier"] = "mil"
        path, asset, variant = self.register_file("unclassified")
        self.save_bundle(asset, variant)
        self.constant_model("logistic_regression", True)
        self.constant_model("mil", False)
        with patch("video_filter.runtime.load_model_manifest", return_value=(signature(), {})):
            task = enqueue(db.session, self.settings, "classify", variant.id)
        with patch("video_filter.worker_client.run_mil_prediction", wraps=run_mil_prediction) as infer:
            operation = classify(db.session, self.settings, task)
            self.assertEqual("source_cleaned", operation.status)
            self.assertFalse(path.exists())
            check_gate(db.session, self.settings, task)
            self.assertEqual(1, infer.call_count)
