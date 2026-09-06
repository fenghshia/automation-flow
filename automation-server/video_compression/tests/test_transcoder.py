import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from video_compression.policy import CompressionPlan
from video_compression.transcoder import build_ffmpeg_command, transcode


class TranscoderTests(unittest.TestCase):
    def test_command_is_argument_list_and_preserves_audio(self):
        plan = CompressionPlan(True, 1920, 1080, True, ("test",))
        command = build_ffmpeg_command(
            Path("ffmpeg.exe"), Path("input name.mp4"), Path("output name.mp4"), plan
        )
        self.assertIsInstance(command, list)
        self.assertIn("hevc_nvenc", command)
        self.assertIn("fps=30,scale=1920:1080:flags=lanczos", command)
        self.assertIn("0:a?", command)
        self.assertEqual("output name.mp4", command[-1])

    @patch("video_compression.transcoder.subprocess.run")
    def test_subprocess_output_is_decoded_safely_on_windows(self, run):
        run.return_value = SimpleNamespace(returncode=0, stderr="")
        plan = CompressionPlan(True, 1920, 1080, False, ("test",))

        transcode("ffmpeg.exe", "input.mp4", "output.mp4", plan, 60)

        self.assertEqual("utf-8", run.call_args.kwargs["encoding"])
        self.assertEqual("replace", run.call_args.kwargs["errors"])


if __name__ == "__main__":
    unittest.main()
