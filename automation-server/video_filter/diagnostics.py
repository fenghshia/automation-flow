"""Read-only environment/media checks. Never import the production app."""

import argparse
import importlib.metadata
import importlib.util
import json
import os
import platform
import shutil
import subprocess
from pathlib import Path


def environment_report(ffmpeg_directory=None):
    os.environ.setdefault("MKL_THREADING_LAYER", "SEQUENTIAL")
    packages = {}
    for name in (
        "torch", "torchvision", "transformers", "timm", "opensmile",
        "numpy", "scikit-learn", "Flask", "Flask-SQLAlchemy", "alembic",
    ):
        try:
            packages[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            packages[name] = None
    report = {"python": platform.python_version(), "packages": packages}
    if importlib.util.find_spec("torch"):
        try:
            import torch

            report["cuda"] = {
                "available": torch.cuda.is_available(),
                "runtime": torch.version.cuda,
            }
            if torch.cuda.is_available():
                report["cuda"].update(
                    device=torch.cuda.get_device_name(0),
                    memory_bytes=torch.cuda.get_device_properties(0).total_memory,
                )
        except Exception as error:
            report["cuda"] = {"error_type": type(error).__name__}
    report["tools"] = {}
    for tool in ("ffmpeg", "ffprobe"):
        executable = (
            Path(ffmpeg_directory) / (tool + ".exe" if platform.system() == "Windows" else tool)
            if ffmpeg_directory else shutil.which(tool)
        )
        if executable:
            try:
                result = subprocess.run(
                    [str(executable), "-version"], capture_output=True,
                    text=True, timeout=15, check=True,
                )
                report["tools"][tool] = result.stdout.splitlines()[0]
            except (OSError, subprocess.SubprocessError):
                report["tools"][tool] = "unavailable"
        else:
            report["tools"][tool] = "unavailable"
    if ffmpeg_directory:
        executable = Path(ffmpeg_directory) / ("ffmpeg.exe" if platform.system() == "Windows" else "ffmpeg")
        try:
            result = subprocess.run([str(executable), "-hide_banner", "-decoders"],
                                    capture_output=True, text=True, timeout=15, check=True)
            report["nvdec_decoders"] = [line.split()[1] for line in result.stdout.splitlines()
                                         if "_cuvid" in line and len(line.split()) >= 2]
        except (OSError, subprocess.SubprocessError):
            report["nvdec_decoders"] = "unavailable"
    return report


def sample_report(source, ffprobe, ffmpeg=None, decode_seconds=0, device="cuda:0"):
    """Report technical metadata, without emitting names, tags, or decoded media."""
    before = source.stat()
    result = subprocess.run(
        [str(ffprobe), "-v", "error", "-show_entries",
         "format=duration,size:stream=index,codec_type,codec_name,width,height,sample_rate,channels",
         "-of", "json", str(source)],
        capture_output=True, text=True, timeout=60, check=True,
    )
    report = json.loads(result.stdout)
    if decode_seconds:
        if not ffmpeg or not 0 < decode_seconds <= 10:
            raise ValueError("A local FFmpeg and a decode duration of at most 10 seconds are required.")
        from .media import MediaDecoder
        decoder = MediaDecoder(Path(ffmpeg).parent, device=device, timeout=60)
        info = decoder.probe(source)
        report["decoded"] = {}
        for name in ("video", "audio"):
            if not any(stream["codec_type"] == name for stream in report["streams"]):
                report["decoded"][name] = "no_track"
                continue
            duration = min(decode_seconds, info["duration_seconds"])
            decoded = (decoder.frames(source, 0, duration, info["video_stream"], count=min(64, max(1, int(duration * 8))))
                       if name == "video" else decoder.audio(source, 0, duration, info["audio_stream"]))
            report["decoded"][name] = {"bytes": decoded.nbytes, "backend": "nvidia_nvdec" if name == "video" else "cpu"}
    after = source.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise RuntimeError("Sample changed during probing.")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", nargs="*", type=Path, default=[])
    parser.add_argument("--group")
    parser.add_argument("--decode-seconds", type=float, default=0)
    args = parser.parse_args()
    # Configuration is read exclusively through EnvConfig. No app/database import.
    from env import EnvConfig
    if args.samples:
        from .group_config import require_scope
        settings = EnvConfig.video_filter_settings(ignore_scope=True)
        group = next((g for g in settings.get("groups", []) if g["name"] == args.group and g["enabled"]), None)
        if group is None:
            raise ValueError("enabled_configured_group_required")
        for path in args.samples:
            require_scope(group, path, sample=True)

    tool_directory = EnvConfig.video_filter_ffmpeg_bin_directory(required=False)
    report = environment_report(tool_directory)
    if args.samples:
        executable = (
            tool_directory / ("ffprobe.exe" if platform.system() == "Windows" else "ffprobe")
            if tool_directory else shutil.which("ffprobe")
        )
        report["samples"] = []
        ffmpeg = (
            tool_directory / ("ffmpeg.exe" if platform.system() == "Windows" else "ffmpeg")
            if tool_directory else shutil.which("ffmpeg")
        )
        for source in args.samples:
            try:
                report["samples"].append(sample_report(source, executable, ffmpeg, args.decode_seconds, device=group["device"]))
            except (OSError, subprocess.SubprocessError, RuntimeError, TypeError, ValueError) as error:
                report["samples"].append({"error_type": type(error).__name__})
    print(json.dumps(report, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
