import hashlib
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from pathlib import Path

from video_filter.features.manifest import load_model_manifest, clear_manifest_cache
from video_filter.features import manifest as manifests
from video_filter.tests.support import signature


class ManifestTests(unittest.TestCase):
    def setUp(self):
        clear_manifest_cache()

    def fixture(self, root):
        artifact = root / "fixture.bin"
        artifact.write_bytes(b"test artifact")
        spec = signature().to_dict()
        for model in spec["models"].values():
            model["artifact_sha256"] = hashlib.sha256(artifact.read_bytes()).hexdigest()
        path = root / "manifest.json"
        path.write_text(json.dumps({"specification": spec, "artifacts": {name: artifact.name for name in spec["models"]}}), encoding="utf-8")
        return path, artifact

    def test_concurrent_validation_runs_once_and_callers_cannot_mutate_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            path, _ = self.fixture(Path(directory))
            with patch.object(manifests, "_verify_manifest", wraps=manifests._verify_manifest) as verify:
                with ThreadPoolExecutor(max_workers=6) as pool:
                    results = list(pool.map(load_model_manifest, [path] * 6))
                verify.assert_called_once()
                results[0][0].models["dino"]["preprocessing_version"] = "corrupted by caller"
                results[0][1].clear()
                loaded, artifacts = load_model_manifest(path)
                self.assertNotEqual("corrupted by caller", loaded.models["dino"]["preprocessing_version"])
                self.assertEqual(4, len(artifacts))
                self.assertEqual(1, verify.call_count)
                load_model_manifest(path, force=True)
                self.assertEqual(2, verify.call_count)

    def test_periodic_recheck_and_replacement_invalidate_success_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            path, artifact = self.fixture(Path(directory))
            with patch.object(manifests, "time") as timer, patch.object(manifests, "_verify_manifest", wraps=manifests._verify_manifest) as verify:
                timer.monotonic.return_value = 0
                load_model_manifest(path)
                timer.monotonic.return_value = 301
                load_model_manifest(path)
                self.assertEqual(2, verify.call_count)
                replacement = artifact.with_suffix(".replacement")
                replacement.write_bytes(b"test artifact")
                replacement.replace(artifact)
                load_model_manifest(path)
                self.assertEqual(3, verify.call_count)

    def test_change_during_validation_is_never_cached(self):
        with tempfile.TemporaryDirectory() as directory:
            path, artifact = self.fixture(Path(directory))
            original = manifests._verify_manifest
            def changed(candidate):
                result = original(candidate)
                artifact.write_bytes(b"changed")
                return result
            with patch.object(manifests, "_verify_manifest", side_effect=changed):
                with self.assertRaisesRegex(ValueError, "changed_during"):
                    load_model_manifest(path)
            with self.assertRaisesRegex(ValueError, "checksum"):
                load_model_manifest(path)

    def test_local_only_loading_checks_actual_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact = root / "fixture.bin"
            artifact.write_bytes(b"test artifact")
            spec = signature().to_dict()
            for model in spec["models"].values():
                model["artifact_sha256"] = hashlib.sha256(artifact.read_bytes()).hexdigest()
            document = {"specification": spec, "artifacts": {name: "fixture.bin" for name in spec["models"]}}
            manifest = root / "manifest.json"
            manifest.write_text(json.dumps(document), encoding="utf-8")
            loaded, paths = load_model_manifest(manifest)
            self.assertEqual(spec, loaded.to_dict())
            self.assertEqual({artifact.resolve()}, set(paths.values()))
            artifact.write_bytes(b"modified artifact")
            with self.assertRaisesRegex(ValueError, "checksum"):
                load_model_manifest(manifest)

    def test_remote_artifact_is_rejected_without_network_access(self):
        with tempfile.TemporaryDirectory() as directory:
            spec = signature().to_dict()
            document = {"specification": spec, "artifacts": {name: "https://example.invalid/weights" for name in spec["models"]}}
            path = Path(directory) / "manifest.json"
            path.write_text(json.dumps(document), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "local"):
                load_model_manifest(path)
