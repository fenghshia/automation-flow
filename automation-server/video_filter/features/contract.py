"""Versioned, JSON-serializable contract shared by extraction and learning."""

import hashlib
import json
import math
import re
from dataclasses import asdict, dataclass


MODALITIES = ("dino", "videomae", "beats", "egemaps")
DIMENSIONS = {"dino": 384, "videomae": 384, "beats": 768, "egemaps": 88}
SCHEMA_VERSION = 1


def canonical_json(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False)


def is_sha256(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


@dataclass(frozen=True)
class FeatureSignature:
    """All feature-space decisions are part of the digest; local paths are excluded.

    Each model pins its artifact hash (including the openSMILE configuration),
    architecture, implementation revision, preprocessing and input specification.
    Windows describe dense VideoMAE sampling and aligned audio/visual positions.
    """

    models: dict
    windows: dict
    aggregation: dict
    schema_version: int = SCHEMA_VERSION

    def to_dict(self):
        result = asdict(self)
        if type(self.schema_version) is not int or self.schema_version != SCHEMA_VERSION:
            raise ValueError("Unsupported feature schema.")
        if set(self.models) != set(MODALITIES):
            raise ValueError("The four required modalities must be specified.")
        for name, spec in self.models.items():
            if not isinstance(spec, dict) or set(spec) != {
                "architecture", "artifact_sha256", "implementation_revision",
                "preprocessing_version", "input", "dimension",
            }:
                raise ValueError("Incomplete model specification.")
            if not is_sha256(spec["artifact_sha256"]):
                raise ValueError("Model artifacts must have a SHA-256 digest.")
            if type(spec["dimension"]) is not int or spec["dimension"] != DIMENSIONS[name]:
                raise ValueError("Unexpected feature dimension for the initial model contract.")
            for key in ("architecture", "implementation_revision", "preprocessing_version"):
                if not isinstance(spec[key], str) or not spec[key].strip():
                    raise ValueError("Model versions cannot be empty.")
            if not isinstance(spec["input"], dict) or not spec["input"]:
                raise ValueError("Model input specification is required.")
        if not isinstance(self.windows, dict) or not self.windows:
            raise ValueError("Window strategy must be specified.")
        if not isinstance(self.aggregation, dict) or not self.aggregation:
            raise ValueError("Aggregation strategy must be specified.")
        # Round-trip isolates nested mutable dictionaries and rejects NaN/Infinity.
        return json.loads(canonical_json(result))

    @property
    def digest(self):
        return hashlib.sha256(canonical_json(self.to_dict()).encode("utf-8")).hexdigest()


def aligned_windows(duration_seconds, window_seconds=10.0):
    """Full non-overlapping coverage; a short final window is explicit."""
    for value in (duration_seconds, window_seconds):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise ValueError("Durations must be finite and positive.")
    return [
        [index * window_seconds, min((index + 1) * window_seconds, duration_seconds)]
        for index in range(math.ceil(duration_seconds / window_seconds))
    ]
