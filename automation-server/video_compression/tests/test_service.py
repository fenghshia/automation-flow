import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from video_compression.service import CompressionService


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


if __name__ == "__main__":
    unittest.main()
