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
        predictions = predict_both(db.session, task)
        return path, asset, variant, bundle, lr, mil, task, predictions

    def feedback(self, asset, label):
        return FeedbackJournal(self.settings["state_directory"]).submit(db.session,
            feedback_values(asset, label, {"reason": "user_confirmed"}))

    def test_two_predictions_persist_idempotently_without_moving_or_labelling(self):
        path, asset, _, _, _, _, task, predictions = self.predict_fixture()
        repeated = predict_both(db.session, task)
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
            self.assertTrue(0 <= score(db.session, mil, bundle) <= 1)
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
        response = app.test_client().get("/video_filter/metrics")
        self.assertEqual(200, response.status_code)
        self.assertEqual(1, response.json["paired"]["reviewed_assets"])
        self.assertNotIn("model_blob", response.get_data(as_text=True))

    def test_feedback_retires_only_the_family_with_a_changed_dataset(self):
        _, asset, _ = self.register_file("confirmed_like")
        lr = self.constant_model("logistic_regression", True, [{"asset_id": asset.id,
            "label": 1, "label_revision": asset.label_revision}])
        mil = self.constant_model("mil", True)
        self.feedback(asset, 0)
        self.assertEqual("retired", lr.status)
        self.assertEqual("active", mil.status)

    def test_lr_retraining_does_not_retire_mil(self):
        mil = self.constant_model("mil", True)
        lr = self.trained_model()
        replacement = train(db.session, signature().digest)
        self.assertEqual("retired", lr.status)
        self.assertEqual("active", replacement.status)
        self.assertEqual("active", mil.status)

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
