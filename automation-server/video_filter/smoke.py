"""Read-only adapter verification on supplied samples, without production DB access."""

import argparse
import json
import os
from pathlib import Path


def main():
    os.environ.setdefault("MKL_THREADING_LAYER", "SEQUENTIAL")
    from env import EnvConfig
    from .extraction import extract
    from .features.manifest import load_model_manifest
    from .media import MediaDecoder

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--group", required=True)
    parser.add_argument("--files", type=Path, nargs="+", required=True)
    parser.add_argument("--seconds", type=float, default=10)
    args = parser.parse_args()
    from .group_config import require_scope
    settings = EnvConfig.video_filter_settings(ignore_scope=True)
    group = next((g for g in settings.get("groups", []) if g["name"] == args.group and g["enabled"]), None)
    if group is None:
        raise ValueError("enabled_configured_group_required")
    for path in args.files:
        require_scope(group, path, sample=True)
    signature, artifacts = load_model_manifest(args.manifest)
    class PreviewDecoder(MediaDecoder):
        def probe(self, path):
            info = super().probe(path)
            info["duration_seconds"] = min(info["duration_seconds"], args.seconds)
            return info
    for index, path in enumerate(args.files):
        result = extract(path, PreviewDecoder(EnvConfig.video_filter_ffmpeg_bin_directory()), signature, artifacts)
        print(json.dumps({"sample_index": index, "preview_seconds": result["duration_seconds"],
            "feature_signature": signature.digest, "dimensions": {name: list(value.shape) for name, value in result["vectors"].items()},
            "measurements": result["measurements"]}), flush=True)


if __name__ == "__main__":
    main()
