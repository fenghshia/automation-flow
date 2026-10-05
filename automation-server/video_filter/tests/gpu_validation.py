"""Explicit group-scoped GPU benchmark in an isolated database; services must stop."""
import argparse
import json
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

from .support import app, db


def main():
    from env import EnvConfig
    from video_filter.scope import group_scope
    from video_filter.group_config import require_scope
    from video_filter.feature_store import FeatureStore
    from video_filter.features.manifest import load_model_manifest
    from video_filter.identity import hash_stable
    from video_filter.models import Asset, Variant
    from video_filter.worker_client import run_extraction
    from media_lineage.resources import gpu_identity, request_lease, release

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--group", required=True)
    parser.add_argument("--files", type=Path, nargs="+", required=True)
    parser.add_argument("--labels", type=int, nargs="+", required=True)
    parser.add_argument("--concurrency", type=int, choices=(1, 2, 4, 6), default=1)
    parser.add_argument("--services-stopped", action="store_true", required=True)
    args = parser.parse_args()
    settings = EnvConfig.video_filter_settings(ignore_scope=True)
    group = next((g for g in settings.get("groups", []) if g["name"] == args.group and g["enabled"]), None)
    if group is None or not settings.get("model_manifest"):
        raise ValueError("configured_group_and_manifest_required")
    if len(args.files) != len(args.labels) or any(label not in (0, 1) for label in args.labels):
        raise ValueError("Each sample requires an explicit binary label.")
    for path in args.files:
        require_scope(group, path, sample=True)
    signature, _ = load_model_manifest(settings["model_manifest"])
    device = gpu_identity(settings["device"])
    with app.app_context(), tempfile.TemporaryDirectory(prefix="video-filter-gpu-benchmark-") as state:
        db.create_all()
        isolated = {**group, "state_directory": Path(state)}
        with group_scope(db.session, isolated) as scoped, ThreadPoolExecutor(max_workers=args.concurrency) as pool:
            requests = []
            for index, (path, label) in enumerate(zip(args.files, args.labels)):
                digest, stat = hash_stable(path)
                asset = Asset(label=label)
                db.session.add(asset)
                db.session.flush()
                variant = Variant(asset_id=asset.id, sha256=digest, size_bytes=stat["size_bytes"])
                db.session.add(variant)
                db.session.commit()
                requests.append((index, {"path": str(path.resolve()), "source_snapshot": stat,
                    "asset_id": asset.id, "variant_id": variant.id, "task_id": str(uuid4()),
                    "feature_signature": signature.digest, "model_manifest": str(settings["model_manifest"]),
                    "ffmpeg_directory": str(settings["ffmpeg_directory"]), "device": settings["device"],
                    "state_directory": str(scoped["state_directory"]), "group_name": args.group,
                    "dataset_group_id": scoped["dataset_group_id"], "reset_epoch": scoped["reset_epoch"],
                    "worker_cpu_threads": settings.get("worker_cpu_threads", 1), "batch_size": settings["batch_size"]}))
            started, running, peak, completed = time.monotonic(), {}, 0, 0
            try:
                while requests or running:
                    for future, (index, lease) in list(running.items()):
                        if not future.done():
                            continue
                        try:
                            prepared, metadata = future.result()
                            stored = FeatureStore().save(db.session, prepared)
                            print(json.dumps({"sample_index": index, "database_summary_verified": True,
                                "windows": stored.manifest["window_count"], "blob_bytes": len(prepared.arrays_blob),
                                "measurements": metadata["measurements"]}), flush=True)
                            completed += 1
                        finally:
                            release(db.session, lease)
                            running.pop(future)
                    if requests and len(running) < args.concurrency:
                        index, request = requests[0]
                        lease = request_lease(db.session, device, "extract_shared", "benchmark:" + request["task_id"], args.concurrency)
                        if lease:
                            request.update(resource_granted=True, resource_lease_id=lease)
                            future = pool.submit(run_extraction, request, settings["task_timeout_seconds"])
                            running[future] = (index, lease)
                            requests.pop(0)
                            peak = max(peak, len(running))
                        elif not running:
                            raise ValueError("insufficient_available_gpu_memory")
                    time.sleep(.1)
            finally:
                for future, (index, lease) in list(running.items()):
                    try:
                        future.result(timeout=settings["task_timeout_seconds"] + 5)
                    finally:
                        release(db.session, lease)
            elapsed = time.monotonic() - started
            print(json.dumps({"configured_concurrency": args.concurrency, "observed_concurrency": peak,
                "completed": completed, "seconds": elapsed, "videos_per_hour": completed * 3600 / elapsed}), flush=True)


if __name__ == "__main__":
    try:
        main()
    finally:
        with app.app_context():
            db.session.remove()
            db.engine.dispose()
