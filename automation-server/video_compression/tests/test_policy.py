import unittest

from video_compression.policy import make_plan, target_dimensions, validate_specification
from video_compression.probe import VideoInfo


class PolicyTests(unittest.TestCase):
    def test_horizontal_and_vertical_dimensions_preserve_ratio(self):
        self.assertEqual((1920, 1080), target_dimensions(3840, 2160))
        self.assertEqual((1080, 1920), target_dimensions(2160, 3840))
        self.assertEqual((1280, 720), target_dimensions(1280, 720))

    def test_source_above_5_mbps_is_transcoded_to_target_specification(self):
        info = VideoInfo("h264", 3840, 2160, 60.0, 47_000_000, 10.0)
        plan = make_plan(info)
        self.assertTrue(plan.transcode)
        self.assertEqual((1920, 1080), (plan.width, plan.height))
        self.assertTrue(plan.cap_fps)

    def test_source_at_5_mbps_is_moved_without_transcoding(self):
        info = VideoInfo("hevc", 1920, 1080, 30.0, 5_000_000, 10.0)
        self.assertFalse(make_plan(info).transcode)
        self.assertEqual([], validate_specification(info))

    def test_low_bitrate_source_ignores_codec_dimensions_and_frame_rate(self):
        info = VideoInfo("h264", 3840, 2160, 60.0, 4_900_000, 10.0)

        self.assertFalse(make_plan(info).transcode)

    def test_high_bitrate_source_transcodes_even_if_other_properties_match(self):
        info = VideoInfo("hevc", 1920, 1080, 30.0, 5_000_001, 10.0)

        self.assertTrue(make_plan(info).transcode)

    def test_unknown_bitrate_fails_instead_of_guessing_about_transcoding(self):
        info = VideoInfo("h264", 3840, 2160, 60.0, None, 10.0)

        with self.assertRaisesRegex(ValueError, "bitrate is unavailable"):
            make_plan(info)

    def test_output_validation_allows_bitrate_up_to_5_5_mbps(self):
        info = VideoInfo("hevc", 1920, 1080, 30.0, 5_500_000, 10.0)
        self.assertEqual([], validate_specification(info))

    def test_output_validation_rejects_bitrate_above_5_5_mbps(self):
        info = VideoInfo("hevc", 1920, 1080, 30.0, 5_500_001, 10.0)
        self.assertEqual(
            ["average video bitrate is 5500001, above 5500000"],
            validate_specification(info),
        )

    def test_rotation_uses_display_dimensions(self):
        info = VideoInfo("h264", 3840, 2160, 30.0, 6_000_000, 10.0, rotation=90)
        plan = make_plan(info)
        self.assertEqual((1080, 1920), (plan.width, plan.height))


if __name__ == "__main__":
    unittest.main()
