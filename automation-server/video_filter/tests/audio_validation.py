"""Explicit local FFmpeg check using generated PCM, never configured media."""

from pathlib import Path
import os
import unittest
from unittest.mock import patch

from env import EnvConfig
from .test_media_recovery import ContinuousAudioTests


def main():
    root = EnvConfig.video_filter_ffmpeg_bin_directory()
    case = ContinuousAudioTests("test_synthetic_pcm_matches_bounded_decoder_when_ffmpeg_is_available")
    binary = Path(root) / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg")
    with patch("video_filter.tests.test_media_recovery.shutil.which", return_value=str(binary)):
        result = unittest.TestResult()
        case.run(result)
    print("synthetic_audio_tests=", result.testsRun, "failures=", len(result.failures), "errors=", len(result.errors))
    for _, detail in result.failures + result.errors:
        print(detail.replace(str(root), "<configured-tools>"))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
