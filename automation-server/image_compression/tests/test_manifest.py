import os
import tempfile
import unittest
from pathlib import Path

from image_compression.manifest import content_manifest, source_snapshot


class ManifestTests(unittest.TestCase):
    def test_content_manifest_changes_with_file_content(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            file_path = root / "a.txt"
            file_path.write_bytes(b"one")
            first = content_manifest(root)
            file_path.write_bytes(b"two")
            second = content_manifest(root)
            self.assertNotEqual(first.digest, second.digest)

    def test_source_snapshot_is_independent_of_creation_order(self):
        with tempfile.TemporaryDirectory() as first_directory, tempfile.TemporaryDirectory() as second_directory:
            first = Path(first_directory)
            second = Path(second_directory)
            (first / "a.txt").write_bytes(b"a")
            (first / "b.txt").write_bytes(b"b")
            (second / "b.txt").write_bytes(b"b")
            (second / "a.txt").write_bytes(b"a")
            for name in ("a.txt", "b.txt"):
                stat_result = (first / name).stat()
                (second / name).touch()
                (second / name).write_bytes((first / name).read_bytes())
                os.utime(second / name, ns=(stat_result.st_atime_ns, stat_result.st_mtime_ns))
            self.assertEqual(source_snapshot(first).digest, source_snapshot(second).digest)


if __name__ == "__main__":
    unittest.main()
