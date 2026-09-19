import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from video_compression.probe import probe_video


class ProbeTests(unittest.TestCase):
    @patch("video_compression.probe.subprocess.run")
    def test_calculates_average_bitrate_from_size_and_duration(self, run_mock):
        run_mock.return_value = SimpleNamespace(
            returncode=0,
            stderr="",
            stdout=json.dumps(
                {
                    "streams": [
                        {
                            "codec_name": "h264",
                            "width": 1920,
                            "height": 1080,
                            "avg_frame_rate": "30/1",
                        }
                    ],
                    "format": {"duration": "10", "size": "5000000"},
                }
            ),
        )

        info = probe_video("example.mp4", "ffprobe.exe")

        self.assertEqual(4_000_000, info.bit_rate)

    @patch("video_compression.probe.subprocess.run")
    def test_prefers_video_stream_bitrate_over_calculated_fallback(self, run_mock):
        run_mock.return_value = SimpleNamespace(
            returncode=0,
            stderr="",
            stdout=json.dumps(
                {
                    "streams": [
                        {
                            "codec_name": "hevc",
                            "width": 1920,
                            "height": 1080,
                            "avg_frame_rate": "30/1",
                            "bit_rate": "3900000",
                        }
                    ],
                    "format": {"duration": "10", "size": "8000000"},
                }
            ),
        )

        info = probe_video("example.mp4", "ffprobe.exe")

        self.assertEqual(3_900_000, info.bit_rate)


if __name__ == "__main__":
    unittest.main()
