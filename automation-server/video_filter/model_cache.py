"""Bounded CPU-only model cache inside a sequential persistent worker."""

import hashlib
import json
from collections import OrderedDict
from pathlib import Path

from logging_config import log_performance


def _identity(path):
    info = path.stat()
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def adapter_key(name, artifact, specification):
    path = Path(artifact).resolve()
    files = [path]
    if name == "videomae":
        files.extend(path.parent / filename for filename in ("config.json", "preprocessor_config.json"))
    elif name == "beats":
        for filename in sorted(specification["input"]["source_hashes"]):
            if Path(filename).name != filename:
                raise ValueError("Invalid BEATs source filename.")
            files.append(path.parent / "beats-source" / filename)
    elif name == "egemaps":
        import opensmile
        import importlib.metadata
        if importlib.metadata.version("opensmile") != specification["input"]["package_version"]:
            raise ValueError("openSMILE version differs from the feature contract.")
        files.extend(sorted((Path(opensmile.__file__).parent / "core" / "config").rglob("*.conf*")))
    value = (name, specification, [(str(file), _identity(file)) for file in files])
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def model_bytes(value):
    if isinstance(value, (tuple, list)):
        return sum(model_bytes(item) for item in value)
    model = getattr(value, "model", value)
    if callable(getattr(model, "parameters", None)):
        tensors = list(model.parameters()) + list(model.buffers())
        if any(tensor.device.type != "cpu" for tensor in tensors):
            raise ValueError("model_cache_requires_cpu")
        return sum(tensor.numel() * tensor.element_size() for tensor in tensors)
    if hasattr(value, "nbytes"):
        return value.nbytes
    # Native openSMILE buffers aren't Python tensors; use a conservative allowance.
    return 64 * 1024**2


class ModelCache:
    def __init__(self, budget_mib):
        self.limit = budget_mib * 1024**2
        self.entries = OrderedDict()
        self.bytes = 0

    def get(self, key, load, *, modality, task_id):
        cached = self.entries.pop(key, None)
        if cached is not None:
            value, size = cached
            model_bytes(value)  # Cached models must be back on CPU between uses.
            self.entries[key] = cached
            hit = True
        else:
            value = load()
            size = model_bytes(value)
            if size <= self.limit:
                while self.entries and self.bytes + size > self.limit:
                    _, (_, removed) = self.entries.popitem(last=False)
                    self.bytes -= removed
                self.entries[key] = (value, size)
                self.bytes += size
            hit = False
        log_performance("model_cache", task_id=task_id, modality=modality, hit=hit,
            entries=len(self.entries), cached_mib=round(self.bytes / 1024**2, 2),
            budget_mib=self.limit // 1024**2, model_mib=round(size / 1024**2, 2))
        return value

    def factories(self, task_id):
        from .features.dino import DinoAdapter
        from .features.videomae import VideoMAEAdapter
        from .features.beats import BeatsAdapter
        from .features.acoustic import AcousticAdapter

        result = {}
        for name, adapter in (("dino", DinoAdapter), ("videomae", VideoMAEAdapter),
                              ("beats", BeatsAdapter), ("egemaps", AcousticAdapter)):
            def factory(artifact, specification, device, name=name, adapter=adapter):
                if device != "cpu":
                    return adapter(artifact, specification, device)
                value = self.get(adapter_key(name, artifact, specification),
                    lambda: adapter(artifact, specification, "cpu"), modality=name, task_id=task_id)
                # OOM limits reflect the current video's concurrency, not all later tasks.
                if hasattr(value, "batch_limit"):
                    del value.batch_limit
                return value
            if hasattr(adapter, "extract_batch"):
                factory.extract_batch = adapter.extract_batch
            factory.__module__ = adapter.__module__
            result[name] = factory
        return result
