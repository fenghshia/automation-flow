import subprocess
import sys
import unittest
from pathlib import Path

from video_filter.features.contract import FeatureSignature, aligned_windows
from video_filter.tests.support import signature


class FeatureContractTests(unittest.TestCase):
    def test_digest_survives_serialization_and_tracks_preprocessing(self):
        spec = signature()
        self.assertEqual(spec.digest, FeatureSignature(**spec.to_dict()).digest)
        changed = spec.to_dict()
        changed["models"]["beats"]["preprocessing_version"] = "changed"
        self.assertNotEqual(spec.digest, FeatureSignature(**changed).digest)

    def test_missing_modality_unknown_digest_or_nan_rejected(self):
        for change in ("missing", "bad-hash", "nan"):
            spec = signature().to_dict()
            if change == "missing":
                del spec["models"]["beats"]
            elif change == "bad-hash":
                spec["models"]["beats"]["artifact_sha256"] = "latest"
            else:
                spec["windows"]["duration_seconds"] = float("nan")
            with self.subTest(change=change), self.assertRaises(ValueError):
                FeatureSignature(**spec).to_dict()

    def test_short_final_window_and_invalid_duration(self):
        self.assertEqual([[0, 10], [10, 12]], aligned_windows(12))
        for duration in (0, -1, True, float("inf")):
            with self.assertRaises(ValueError):
                aligned_windows(duration)

    def test_package_import_has_no_application_or_gpu_imports(self):
        result = subprocess.run(
            [sys.executable, "-c", "import sys; import video_filter; import media_lineage; "
             "assert 'app' not in sys.modules; assert 'torch' not in sys.modules"],
            cwd=Path(__file__).parents[2], capture_output=True, text=True, timeout=15,
        )
        self.assertEqual(0, result.returncode, result.stderr)
