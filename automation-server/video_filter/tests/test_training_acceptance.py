import unittest
import numpy as np

from video_filter.tests.support import app  # Install the isolated app before model imports.
from video_filter.evaluation import acceptance_result, classification_metrics
from video_filter.training_config import digest, effective, validate_acceptance
from video_filter.group_config import training
from video_filter.learning import select_threshold


class AcceptanceTests(unittest.TestCase):
    def test_each_label_uses_its_own_denominator(self):
        metrics = classification_metrics([1, 1, 1, 0, 0, 0, 0], [1, 1, 0, 1, 0, 0, 0])
        self.assertEqual({"actual_assets": 3, "predicted_assets": 3, "correct": 2,
            "false_predictions": 1, "missed_assets": 1, "precision": 2 / 3, "recall": 2 / 3}, metrics["labels"]["like"])
        self.assertEqual(4, metrics["labels"]["dislike"]["predicted_assets"])
        self.assertEqual(.75, metrics["labels"]["dislike"]["precision"])
        result = acceptance_result(metrics, {"like_precision": .6, "dislike_precision": .8})
        self.assertEqual({"like": True, "dislike": False}, result["checks"])
        self.assertFalse(result["passed"])

    def test_missing_prediction_class_cannot_pass_even_zero_gate(self):
        metrics = classification_metrics([0, 1], [0, 0])
        self.assertIsNone(metrics["labels"]["like"]["precision"])
        self.assertEqual(0, metrics["labels"]["like"]["recall"])
        self.assertFalse(acceptance_result(metrics, {"like_precision": 0, "dislike_precision": 0})["passed"])

    def test_boundaries_and_separate_thresholds_apply_to_both_models(self):
        metrics = classification_metrics([0, 1], [0, 1])
        self.assertTrue(acceptance_result(metrics, {"like_precision": 1, "dislike_precision": 1})["passed"])
        for kind in ("logistic_regression", "mil"):
            params = effective({}, kind, acceptance={"like_precision": .7, "dislike_precision": .9})
            self.assertEqual(.7, params["activation_like_precision"])
            self.assertEqual(.9, params["activation_dislike_precision"])
            self.assertNotEqual(digest({}, kind), digest({}, kind, acceptance={"like_precision": .7, "dislike_precision": .9}))

    def test_invalid_thresholds_are_rejected(self):
        for value in (None, [], {}, {"like_precision": .8},
                      {"like_precision": True, "dislike_precision": .8},
                      {"like_precision": "80", "dislike_precision": .8},
                      {"like_precision": float("nan"), "dislike_precision": .8},
                      {"like_precision": .8, "dislike_precision": 1.01}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_acceptance(value)
        for value in (-.1, 1.1, True, float("inf")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                training({"activation_like_precision": value})

    def test_old_gates_remain_readable_but_do_not_control_new_acceptance(self):
        settings = {"training": {"activation_balanced_accuracy": 1, "activation_roc_auc": 1}}
        for kind in ("logistic_regression", "mil"):
            self.assertEqual(effective({}, kind), effective(settings, kind))
            self.assertEqual(digest({}, kind), digest(settings, kind))

    def test_threshold_selection_prefers_candidate_passing_both_labels(self):
        labels = np.array([1, 1, 1, 1, 0, 0, 0, 0])
        scores = np.array([.9, .8, .7, .4, .6, .3, .2, .1])
        _, metrics, result = select_threshold(labels, scores, {"like_precision": .95, "dislike_precision": .75})
        self.assertTrue(result["passed"])
        self.assertEqual(1, metrics["labels"]["like"]["precision"])
        self.assertEqual(.8, metrics["labels"]["dislike"]["precision"])

    def test_no_qualified_threshold_reports_failure_instead_of_inventing_precision(self):
        _, metrics, result = select_threshold(np.array([0, 1]), np.array([.5, .5]),
            {"like_precision": .8, "dislike_precision": .8})
        self.assertFalse(result["passed"])
        self.assertTrue(any(row["precision"] is None for row in metrics["labels"].values()))
