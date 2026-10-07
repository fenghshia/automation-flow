"""Verify explicitly supplied local artifacts; there is no download fallback."""

import hashlib
import json
import copy
import threading
import time
from collections import OrderedDict
from pathlib import Path

from .contract import MODALITIES, FeatureSignature


_cache = OrderedDict()
_cache_lock = threading.Lock()
_recheck_seconds = 300


def _identity(path):
    info = path.stat()
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def clear_manifest_cache():
    with _cache_lock:
        _cache.clear()


def load_model_manifest(manifest_path, *, force=False):
    path = Path(manifest_path).resolve()
    with _cache_lock:
        cached = _cache.get(path)
        if cached and not force and time.monotonic() - cached[0] < _recheck_seconds:
            if cached[1] == _identity(path) and all(identity == _identity(artifact) for artifact, identity in cached[2].items()):
                _cache.move_to_end(path)
                return copy.deepcopy(cached[3]), dict(cached[4])
        _cache.pop(path, None)
        before = _identity(path)
        signature, artifacts, identities = _verify_manifest(path)
        if before != _identity(path) or any(identity != _identity(artifact) for artifact, identity in identities.items()):
            raise ValueError("model_manifest_changed_during_validation")
        _cache[path] = (time.monotonic(), before, identities, copy.deepcopy(signature), dict(artifacts))
        while len(_cache) > 16:
            _cache.popitem(last=False)
        return signature, artifacts


def _verify_manifest(manifest_path):
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
    artifacts, identities = {}, {}
    for name, value in content["artifacts"].items():
        if not isinstance(value, str) or not value.strip() or "://" in value:
            raise ValueError("Only explicit local artifact paths are supported.")
        artifact = Path(value)
        if not artifact.is_absolute():
            artifact = path.parent / artifact
        artifact = artifact.resolve()
        before = _identity(artifact)
        digest = hashlib.sha256()
        with artifact.open("rb") as source:
            for block in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(block)
        if digest.hexdigest() != signature.models[name]["artifact_sha256"]:
            raise ValueError("Local model artifact checksum mismatch.")
        if before != _identity(artifact):
            raise ValueError("model_artifact_changed_during_validation")
        artifacts[name] = artifact
        identities[artifact] = before
    return signature, artifacts, identities
