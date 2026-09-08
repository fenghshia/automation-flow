import io
import unittest
from pathlib import Path
from unittest.mock import Mock
from unittest.mock import patch

from video_compression.policy import CompressionPlan
from video_compression.transcoder import TranscodeError, build_ffmpeg_command, transcode


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

    @patch("video_compression.transcoder.subprocess.Popen")
    def test_subprocess_output_is_decoded_safely_on_windows(self, popen):
        process = Mock()
        process.stdout = io.StringIO("progress=end\n")
        process.wait.return_value = 0
        popen.return_value = process
        plan = CompressionPlan(True, 1920, 1080, False, ("test",))

        transcode("ffmpeg.exe", "input.mp4", "output.mp4", plan, 60)

        self.assertEqual("utf-8", popen.call_args.kwargs["encoding"])
        self.assertEqual("replace", popen.call_args.kwargs["errors"])

    @patch("video_compression.transcoder.subprocess.Popen")
    def test_progress_is_logged_from_ffmpeg_output(self, popen):
        process = Mock()
        process.stdout = io.StringIO(
            "out_time=00:00:10.000000\n"
            "progress=continue\n"
            "out_time=00:00:50.000000\n"
            "progress=continue\n"
            "progress=end\n"
        )
        process.wait.return_value = 0
        popen.return_value = process
        plan = CompressionPlan(True, 1920, 1080, False, ("test",))

        with self.assertLogs("video_compression.transcoder", level="INFO") as logs:
            transcode(
                "ffmpeg.exe",
                "input.mp4",
                "output.mp4",
                plan,
                60,
                duration=100,
            )

        output = "\n".join(logs.output)
        self.assertIn("progress=10%", output)
        self.assertIn("progress=50%", output)
        self.assertIn("视频转码完成", output)

    @patch("video_compression.transcoder.subprocess.Popen")
    def test_failure_includes_tail_of_ffmpeg_stderr(self, popen):
        process = Mock()
        process.stdout = io.StringIO("progress=end\n")
        process.wait.return_value = 1

        def start_process(*args, **kwargs):
            kwargs["stderr"].write("encoder failure marker")
            kwargs["stderr"].flush()
            return process

        popen.side_effect = start_process
        plan = CompressionPlan(True, 1920, 1080, False, ("test",))

        with self.assertRaisesRegex(TranscodeError, "encoder failure marker"):
            transcode("ffmpeg.exe", "input.mp4", "output.mp4", plan, 60)


if __name__ == "__main__":
    unittest.main()
