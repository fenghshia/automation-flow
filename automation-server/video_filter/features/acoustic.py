"""Local eGeMAPSv02 Functionals with explicit unvoiced/invalid statistics."""

import hashlib
import importlib.metadata
from pathlib import Path

import numpy as np


class AcousticAdapter:
    def __init__(self, artifact, specification, device="cpu"):
        import opensmile

        config = specification["input"]
        if importlib.metadata.version("opensmile") != config["package_version"]:
            raise ValueError("openSMILE version differs from the feature contract.")
        root = Path(opensmile.__file__).parent / "core" / "config"
        digest = hashlib.sha256()
        for path in sorted(root.rglob("*.conf*")):
            digest.update(path.relative_to(root).as_posix().encode("utf-8"))
            digest.update(hashlib.sha256(path.read_bytes()).digest())
        if digest.hexdigest() != config["config_tree_sha256"]:
            raise ValueError("openSMILE configuration differs from the feature contract.")
        self.smile = opensmile.Smile(feature_set=opensmile.FeatureSet.eGeMAPSv02,
                                    feature_level=opensmile.FeatureLevel.Functionals)

    def extract(self, samples):
        # Pad only very short tails to allow the acoustic analysis frame to form.
        if len(samples) < 640:
            samples = np.pad(samples, (0, 640 - len(samples)))
        output = self.smile.process_signal(samples, 16000)
        if len(output) != 1 or output.shape[1] != 88:
            raise ValueError("Unexpected eGeMAPSv02 output shape.")
        values = output.iloc[0].to_numpy(dtype=np.float32, copy=True)
        valid = np.isfinite(values)
        columns = list(output.columns)
        voiced = output.iloc[0].get("VoicedSegmentsPerSec", 0) > 0
        if not voiced:
            for index, name in enumerate(columns):
                if any(part in name for part in ("F0", "jitter", "shimmer", "HNR", "F1", "F2", "F3")):
                    valid[index] = False
        values[~valid] = 0
        return values, valid
