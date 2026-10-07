"""Bounded adapter batches, preserving ordering and retrying smaller OOM batches."""

import numpy as np
from logging_config import log_performance


def infer_batches(adapter, items, batch_size, *, modality, task_id=None):
    result = []
    limit = min(batch_size, getattr(adapter, "batch_limit", batch_size))
    offset = 0
    while offset < len(items):
        part = items[offset:offset + limit]
        # Catch after the adapter frame unwinds so failed GPU inputs are freed.
        failed = False
        try:
            value = adapter.extract_batch(part)
        except adapter.torch.cuda.OutOfMemoryError:
            if len(part) == 1:
                raise  # Preserve the original model traceback for error.log.
            failed = True
        if failed:
            adapter.torch.cuda.empty_cache()
            limit = max(1, len(part) // 2)
            adapter.batch_limit = limit
            log_performance("inference_batch_reduced", task_id=task_id, modality=modality,
                attempted_size=len(part), next_size=limit)
            continue
        value = np.asarray(value, dtype=np.float32)
        if value.ndim != 2 or len(value) != len(part) or not np.isfinite(value).all():
            raise ValueError("Invalid batched features.")
        result.append(value)
        log_performance("inference_batch", task_id=task_id, modality=modality,
            requested_size=batch_size, actual_size=len(part))
        offset += len(part)
    return np.concatenate(result)


def activate(adapter, device):
    adapter.device = device
    adapter.model.to(device)
    if hasattr(adapter, "mean"):
        adapter.mean = adapter.mean.to(device)
        adapter.std = adapter.std.to(device)


def deactivate(adapter):
    activate(adapter, "cpu")
    adapter.torch.cuda.empty_cache()
