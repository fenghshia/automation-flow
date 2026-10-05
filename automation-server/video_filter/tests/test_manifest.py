import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from video_filter.features.manifest import load_model_manifest
from video_filter.tests.support import signature


class ManifestTests(unittest.TestCase):
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
