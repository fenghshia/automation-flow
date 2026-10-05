"""Explicit local artifact preparation; inference never calls this downloader."""

import argparse
import hashlib
import json
import os
import urllib.request
from pathlib import Path


BEATS_REVISION = "31c5b904ca1bf2afb4c234a6675c683a4e5fc7cd"
VIDEO_REPOSITORY = "MCG-NJU/videomae-small-finetuned-kinetics"
VIDEO_REVISION = "1bf2b82c809609f5df1ea7648aee577e2e486b63"


def download(opener, url, destination):
    if destination.is_file() and destination.stat().st_size > 0:
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".download")
    try:
        with opener.open(url, timeout=60) as response, temporary.open("wb") as output:
            for block in iter(lambda: response.read(1024 * 1024), b""):
                output.write(block)
            output.flush()
            os.fsync(output.fileno())
        os.rename(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def digest(path):
    result = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def provision(root, proxy=None):
    import importlib.metadata
    import opensmile
    from .features.contract import DIMENSIONS, FeatureSignature, canonical_json

    root = root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    handler = urllib.request.ProxyHandler({"http": proxy, "https": proxy} if proxy else {})
    opener = urllib.request.build_opener(handler)
    with opener.open("https://huggingface.co/api/models/" + VIDEO_REPOSITORY + "/revision/" + VIDEO_REVISION + "?blobs=true", timeout=60) as response:
        repository = json.load(response)
    video_revision = repository["sha"]
    if video_revision != VIDEO_REVISION:
        raise ValueError("VideoMAE repository revision mismatch.")
    artifacts = {
        "dino": root / "dinov2_vits14_pretrain.pth",
        "videomae": root / "videomae" / "pytorch_model.bin",
        "beats": root / "BEATs_iter3.pt",
    }
    download(opener, "https://dl.fbaipublicfiles.com/dinov2/dinov2_vits14/dinov2_vits14_pretrain.pth", artifacts["dino"])
    for filename in ("pytorch_model.bin", "config.json", "preprocessor_config.json"):
        download(opener, f"https://huggingface.co/{VIDEO_REPOSITORY}/resolve/{video_revision}/{filename}", root / "videomae" / filename)
    video_expected = next(item["lfs"]["sha256"] for item in repository["siblings"] if item["rfilename"] == "pytorch_model.bin")
    if digest(artifacts["videomae"]) != video_expected:
        raise ValueError("VideoMAE checkpoint checksum mismatch.")
    # Official Azure hosting currently denies public access. Use a revision-pinned
    # mirror of the pretrained checkpoint, verified against its LFS SHA-256.
    mirror = "https://huggingface.co/api/models/lpepino/beats_ckpts/revision/5b53b0404df452a3a607d7e67687227730e5bad1?blobs=true"
    with opener.open(mirror, timeout=60) as response:
        mirror_metadata = json.load(response)
    expected = next(item["lfs"]["sha256"] for item in mirror_metadata["siblings"] if item["rfilename"] == "BEATs_iter3.pt")
    download(opener, "https://huggingface.co/lpepino/beats_ckpts/resolve/5b53b0404df452a3a607d7e67687227730e5bad1/BEATs_iter3.pt", artifacts["beats"])
    if digest(artifacts["beats"]) != expected:
        raise ValueError("BEATs mirrored artifact checksum mismatch.")
    source_hashes = {}
    for filename in ("BEATs.py", "backbone.py", "modules.py"):
        target = root / "beats-source" / filename
        download(opener, f"https://raw.githubusercontent.com/microsoft/unilm/{BEATS_REVISION}/beats/{filename}", target)
        source_hashes[filename] = digest(target)
    # Pin the full openSMILE config tree, not just its top-level include file.
    config_root = Path(opensmile.__file__).parent / "core" / "config"
    tree = hashlib.sha256()
    for path in sorted(config_root.rglob("*.conf*")):
        tree.update(path.relative_to(config_root).as_posix().encode("utf-8"))
        tree.update(bytes.fromhex(digest(path)))
    acoustic = root / "egemaps-config-signature.json"
    acoustic.write_text(canonical_json({"feature_set": "eGeMAPSv02", "dimension": 88,
        "package_version": importlib.metadata.version("opensmile"), "config_tree_sha256": tree.hexdigest()}), encoding="utf-8")
    artifacts["egemaps"] = acoustic
    inputs = {
        "dino": {"height": 224, "width": 224, "frames_per_window": 3,
                 "mean": [0.485, 0.456, 0.406], "std": [0.229, 0.224, 0.225],
                 "package_version": importlib.metadata.version("timm")},
        "videomae": {"height": 224, "width": 224, "num_frames": 16, "fps": 8,
                     "num_attention_heads": 6,
                     "package_version": importlib.metadata.version("transformers"),
                     "config_sha256": digest(root / "videomae" / "config.json"),
                     "preprocessor_sha256": digest(root / "videomae" / "preprocessor_config.json"),
                     "revision": video_revision},
        "beats": {"sample_rate": 16000, "max_seconds": 5, "source_revision": BEATS_REVISION,
                  "source_hashes": source_hashes},
        "egemaps": {"sample_rate": 16000, "feature_set": "eGeMAPSv02", "feature_level": "Functionals",
                    "package_version": importlib.metadata.version("opensmile"), "config_tree_sha256": tree.hexdigest()},
    }
    architectures = {"dino": "dinov2-vits14", "videomae": "videomae-small-k400",
                     "beats": "BEATs-iter3-pretrained", "egemaps": "eGeMAPSv02"}
    for value in inputs.values():
        value["torch_version"] = importlib.metadata.version("torch")
    for name in ("dino", "videomae"):
        inputs[name]["spatial_preprocessing"] = "scale-short-side-256-center-crop-224-fps-round-up-still-direct"
    signature = FeatureSignature(
        models={name: {"architecture": architectures[name], "artifact_sha256": digest(path),
                      "implementation_revision": "video_filter-v1", "preprocessing_version": "aligned-v1-fp32",
                      "input": inputs[name], "dimension": DIMENSIONS[name]} for name, path in artifacts.items()},
        windows={"strategy": "full-aligned", "duration_seconds": 10.0},
        aggregation={"method": "validity-aware-mean-std", "audio_chunk_mean": True},
    )
    manifest = root / "manifest.json"
    manifest.write_text(canonical_json({"specification": signature.to_dict(),
        "artifacts": {name: path.relative_to(root).as_posix() for name, path in artifacts.items()}}), encoding="utf-8")
    print(json.dumps({"prepared": True, "feature_signature": signature.digest}))
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--proxy")
    args = parser.parse_args()
    provision(args.directory, args.proxy)


if __name__ == "__main__":
    main()
