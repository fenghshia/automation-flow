"""Verify explicitly supplied local artifacts; there is no download fallback."""

import hashlib
import json
from pathlib import Path

from .contract import MODALITIES, FeatureSignature


def load_model_manifest(manifest_path):
    path = Path(manifest_path).resolve()
    if path.stat().st_size > 1024 * 1024:
        raise ValueError("Model manifest exceeds the size limit.")
    content = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(content, dict) or set(content) != {"specification", "artifacts"}:
        raise ValueError("Model manifest requires specification and local artifacts.")
    signature = FeatureSignature(**content["specification"])
    signature.to_dict()
    if not isinstance(content["artifacts"], dict) or set(content["artifacts"]) != set(MODALITIES):
        raise ValueError("All local artifacts must be supplied.")
    artifacts = {}
    for name, value in content["artifacts"].items():
        if not isinstance(value, str) or not value.strip() or "://" in value:
            raise ValueError("Only explicit local artifact paths are supported.")
        artifact = Path(value)
        if not artifact.is_absolute():
            artifact = path.parent / artifact
        artifact = artifact.resolve()
        digest = hashlib.sha256()
        with artifact.open("rb") as source:
            for block in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(block)
        if digest.hexdigest() != signature.models[name]["artifact_sha256"]:
            raise ValueError("Local model artifact checksum mismatch.")
        artifacts[name] = artifact
    return signature, artifacts
