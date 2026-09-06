import unittest

from video_compression.policy import make_plan, target_dimensions, validate_specification
from video_compression.probe import VideoInfo


class PolicyTests(unittest.TestCase):
    def test_horizontal_and_vertical_dimensions_preserve_ratio(self):
        self.assertEqual((1920, 1080), target_dimensions(3840, 2160))
        self.assertEqual((1080, 1920), target_dimensions(2160, 3840))
        self.assertEqual((1280, 720), target_dimensions(1280, 720))

    def test_noncompliant_source_is_transcoded(self):
        info = VideoInfo("h264", 3840, 2160, 60.0, 47_000_000, 10.0)
        plan = make_plan(info)
        self.assertTrue(plan.transcode)
        self.assertEqual((1920, 1080), (plan.width, plan.height))
        self.assertTrue(plan.cap_fps)

    def test_compliant_hevc_source_can_be_moved_directly(self):
        info = VideoInfo("hevc", 1920, 1080, 30.0, 4_900_000, 10.0)
        self.assertFalse(make_plan(info).transcode)
        self.assertEqual([], validate_specification(info))

    def test_rotation_uses_display_dimensions(self):
        info = VideoInfo("h264", 3840, 2160, 30.0, 6_000_000, 10.0, rotation=90)
        plan = make_plan(info)
        self.assertEqual((1080, 1920), (plan.width, plan.height))


if __name__ == "__main__":
    unittest.main()

