import os
import tempfile
import unittest
from contextlib import chdir
from pathlib import Path
from unittest.mock import patch

from env import EnvConfig


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.variables = {"VIDEO_FILTER_ENABLED": "true"}
        for suffix in ("CONFIRMED_LIKE_DIR", "PREDICTED_LIKE_DIR", "PREDICTED_DISLIKE_DIR", "UNCLASSIFIED_DIR", "STATE_DIR"):
            self.variables["VIDEO_FILTER_" + suffix] = str(root / suffix)
        self.variables["VIDEO_COMPRESSION_OUTPUT_DIR"] = str(root / "compressed")
        self.environment = patch.dict(os.environ, self.variables, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.loader = patch.object(EnvConfig, "_loaded", True)
        self.loader.start()
        self.addCleanup(self.loader.stop)

    def test_disabled_without_any_other_configuration(self):
        with patch.dict(os.environ, {"VIDEO_FILTER_ENABLED": "false"}, clear=True):
            self.assertEqual({"enabled": False}, EnvConfig.video_filter_settings())

    def test_five_roles_and_independent_storage(self):
        settings = EnvConfig.video_filter_settings()
        self.assertEqual(set(settings["directories"]), {
            "confirmed_like", "predicted_like", "predicted_dislike", "unclassified", "compressed_like",
        })
        self.assertFalse(settings["transfer_enabled"])
        self.assertFalse(settings["state_directory"].exists())

    def test_missing_directory_fails_when_enabled(self):
        del os.environ["VIDEO_FILTER_UNCLASSIFIED_DIR"]
        with self.assertRaisesRegex(RuntimeError, "UNCLASSIFIED"):
            EnvConfig.video_filter_settings()

    def test_nested_and_alias_paths_rejected(self):
        for value in (self.variables["VIDEO_FILTER_CONFIRMED_LIKE_DIR"],
                      str(Path(self.variables["VIDEO_FILTER_CONFIRMED_LIKE_DIR"]) / "child"),
                      str(Path(self.variables["VIDEO_FILTER_CONFIRMED_LIKE_DIR"]) / "child" / "..")):
            with self.subTest(value=value), patch.dict(os.environ, {"VIDEO_FILTER_PREDICTED_LIKE_DIR": value}):
                with self.assertRaisesRegex(RuntimeError, "overlap"):
                    EnvConfig.video_filter_settings()

    def test_state_nested_in_video_directory_rejected(self):
        os.environ["VIDEO_FILTER_STATE_DIR"] = str(Path(self.variables["VIDEO_FILTER_UNCLASSIFIED_DIR"]) / "state")
        with self.assertRaisesRegex(RuntimeError, "overlap"):
            EnvConfig.video_filter_settings()

    def test_directory_pointing_to_file_rejected(self):
        path = Path(self.variables["VIDEO_FILTER_UNCLASSIFIED_DIR"])
        path.write_bytes(b"test")
        with self.assertRaisesRegex(RuntimeError, "directory"):
            EnvConfig.video_filter_settings()

    def test_lineage_allows_independent_unknown_compression_source(self):
        os.environ["VIDEO_FILTER_LINEAGE_ENABLED"] = "true"
        os.environ["VIDEO_COMPRESSION_SOURCE_DIR"] = str(Path(self.temp.name) / "other-source")
        settings = EnvConfig.video_filter_settings()
        self.assertTrue(settings["lineage_enabled"])
        self.assertNotIn("compression_source_directory", settings)
        self.assertNotEqual(EnvConfig.video_compression_source_directory(), settings["directories"]["confirmed_like"])

    def test_lineage_also_allows_source_in_a_managed_directory(self):
        os.environ["VIDEO_FILTER_LINEAGE_ENABLED"] = "true"
        os.environ["VIDEO_COMPRESSION_SOURCE_DIR"] = self.variables["VIDEO_FILTER_CONFIRMED_LIKE_DIR"]
        settings = EnvConfig.video_filter_settings()
        self.assertTrue(settings["lineage_enabled"])
        source = settings["directories"]["confirmed_like"]
        output = settings["directories"]["compressed_like"]
        self.assertEqual(EnvConfig.video_compression_source_directory(), source)
        self.assertEqual(EnvConfig.video_compression_output_directory(), output)
        self.assertNotEqual(source, output)

    def test_independent_compression_source_must_not_overlap_output_or_state(self):
        os.environ["VIDEO_FILTER_LINEAGE_ENABLED"] = "true"
        for value in (self.variables["VIDEO_COMPRESSION_OUTPUT_DIR"],
                      self.variables["VIDEO_FILTER_STATE_DIR"],
                      str(Path(self.variables["VIDEO_FILTER_CONFIRMED_LIKE_DIR"]) / "nested-source")):
            with self.subTest(value=value), patch.dict(os.environ, {"VIDEO_COMPRESSION_SOURCE_DIR": value}):
                with self.assertRaises(RuntimeError):
                    EnvConfig.video_filter_settings()

    def test_relative_state_and_manifest_paths_use_server_directory_not_cwd(self):
        server = (Path(self.temp.name) / "automation-server").resolve()
        with patch.object(EnvConfig, "_project_dir", server), chdir(self.temp.name), patch.dict(os.environ, {
            "VIDEO_FILTER_STATE_DIR": "video_filter/state",
            "VIDEO_FILTER_MODEL_MANIFEST": "video_filter/weights/manifest.json",
        }):
            settings = EnvConfig.video_filter_settings()
        self.assertEqual(server / "video_filter" / "state", settings["state_directory"])
        self.assertEqual(server / "video_filter" / "weights" / "manifest.json", settings["model_manifest"])
        self.assertFalse(settings["state_directory"].exists())

    def test_invalid_boolean_and_timing_fail(self):
        for name, value in (("VIDEO_FILTER_TRANSFER_ENABLED", "yes"),
                            ("VIDEO_FILTER_MISSING_SECONDS", "0"),
                            ("VIDEO_FILTER_STABLE_SECONDS", "abc"),
                            ("VIDEO_FILTER_BATCH_SIZE", "-1"),
                            ("VIDEO_FILTER_DEVICE", "gpu")):
            with self.subTest(name=name), patch.dict(os.environ, {name: value}):
                with self.assertRaises(RuntimeError):
                    EnvConfig.video_filter_settings()

    def test_classifier_selection_and_mil_settings(self):
        self.assertEqual("logistic_regression", EnvConfig.video_filter_settings()["classifier"])
        with patch.dict(os.environ, {"VIDEO_FILTER_CLASSIFIER": "mil", "VIDEO_FILTER_MIL_EPOCHS": "12"}):
            settings = EnvConfig.video_filter_settings()
            self.assertEqual("mil", settings["classifier"])
            self.assertEqual(12, settings["mil_epochs"])
        for name, value in (("VIDEO_FILTER_CLASSIFIER", "auto"), ("VIDEO_FILTER_MIL_PATIENCE", "0"),
                            ("VIDEO_FILTER_MIL_EPOCHS", "invalid")):
            with patch.dict(os.environ, {name: value}), self.assertRaises(RuntimeError):
                EnvConfig.video_filter_settings()
