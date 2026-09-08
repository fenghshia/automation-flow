import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from video_compression.service import CompressionError, CompressionService


class PublishingSafetyTests(unittest.TestCase):
    def test_validation_failure_preserves_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.mp4"
            destination = root / "output.mp4"
            source.write_bytes(b"video")
            service = CompressionService(root, root)
            service.validate_output = Mock(side_effect=RuntimeError("invalid"))

            with self.assertRaisesRegex(RuntimeError, "invalid"):
                service._publish_and_remove_source(source, source, destination)

            self.assertTrue(source.exists())
            self.assertFalse(destination.exists())

    def test_success_publishes_same_bytes_then_removes_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "same name.mp4"
            destination = root / "output" / source.name
            destination.parent.mkdir()
            source.write_bytes(b"video")
            service = CompressionService(root, destination.parent)
            service.validate_output = Mock(return_value=None)
            callback = Mock()

            service._publish_and_remove_source(
                source, source, destination, callback
            )

            self.assertFalse(source.exists())
            self.assertEqual(b"video", destination.read_bytes())
            callback.assert_called_once_with(destination)

    def test_existing_destination_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.mp4"
            destination = root / "output.mp4"
            source.write_bytes(b"new")
            destination.write_bytes(b"old")
            service = CompressionService(root, root)
            service.validate_output = Mock(return_value=None)

            with self.assertRaises(FileExistsError):
                service._publish_and_remove_source(source, source, destination)

            self.assertEqual(b"new", source.read_bytes())
            self.assertEqual(b"old", destination.read_bytes())


class RecoverablePublishingTests(unittest.TestCase):
    def make_service(self, root):
        output = root / "output"
        cache = root / "cache"
        output.mkdir()
        cache.mkdir()
        service = CompressionService(root, output, cache)
        service.validate_output = Mock(return_value=None)
        return service

    def test_task_paths_are_deterministic_and_confined(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = self.make_service(root)

            destination = service.destination_for("example.mp4")

            self.assertEqual(
                service.output_dir.resolve() / "example.mp4", destination
            )
            self.assertEqual(
                service.cache_dir.resolve() / ".mission-7.transcoding.mp4",
                service.work_path_for(7),
            )
            self.assertEqual(
                service.output_dir.resolve() / ".example.mp4.mission-7.part",
                service.staging_path_for(7, destination),
            )
            with self.assertRaisesRegex(CompressionError, "file name is invalid"):
                service.destination_for("../example.mp4")

    def test_publish_prepared_creates_same_file_without_removing_staging(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = self.make_service(root)
            destination = service.destination_for("example.mp4")
            staging = service.staging_path_for(7, destination)
            staging.write_bytes(b"video")

            published_staging = service.publish_prepared(7, destination)

            self.assertEqual(staging, published_staging)
            self.assertTrue(staging.samefile(destination))
            self.assertEqual(b"video", destination.read_bytes())

    def test_publish_prepared_never_overwrites_existing_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = self.make_service(root)
            destination = service.destination_for("example.mp4")
            staging = service.staging_path_for(7, destination)
            staging.write_bytes(b"new")
            destination.write_bytes(b"old")

            with self.assertRaises(FileExistsError):
                service.publish_prepared(7, destination)

            self.assertEqual(b"new", staging.read_bytes())
            self.assertEqual(b"old", destination.read_bytes())

    def test_prepare_creates_recoverable_staging_before_publication(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = self.make_service(root)
            (root / "ffmpeg.exe").write_bytes(b"")
            (root / "ffprobe.exe").write_bytes(b"")
            source_directory = root / "source"
            source_directory.mkdir()
            source = source_directory / "example.mp4"
            source.write_bytes(b"video")
            destination = service.destination_for(source.name)

            with patch(
                "video_compression.service.probe_video",
                return_value=SimpleNamespace(
                    codec_name="hevc",
                    display_width=1920,
                    display_height=1080,
                    fps=30.0,
                    bit_rate=4_500_000,
                ),
            ), patch(
                "video_compression.service.make_plan",
                return_value=SimpleNamespace(transcode=False),
            ):
                prepared = service.prepare(source, 7, destination)

            self.assertTrue(source.exists())
            self.assertFalse(destination.exists())
            self.assertEqual(b"video", prepared.staging.read_bytes())
            service.validate_output.assert_called_once_with(source.resolve())


if __name__ == "__main__":
    unittest.main()
