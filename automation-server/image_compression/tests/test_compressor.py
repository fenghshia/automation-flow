import importlib.util
import os
import tempfile
import unittest
from pathlib import Path

from image_compression.compressor import ImageProcessor
from image_compression.policy import MAX_IMAGE_BYTES


@unittest.skipUnless(importlib.util.find_spec("PIL"), "Pillow is not installed")
class PillowCompressionTests(unittest.TestCase):
    def test_small_png_is_copied_byte_for_byte(self):
        from PIL import Image

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.png"
            destination = root / "destination.png"
            Image.new("RGB", (16, 16), "red").save(source)
            original = source.read_bytes()

            transcoded = ImageProcessor().process(source, destination)

            self.assertFalse(transcoded)
            self.assertEqual(original, destination.read_bytes())

    def test_large_jpeg_is_reduced_below_limit(self):
        from PIL import Image

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.jpg"
            destination = root / "destination.jpg"
            image = Image.frombytes("RGB", (2200, 2200), os.urandom(2200 * 2200 * 3))
            image.save(source, quality=100)
            self.assertGreater(source.stat().st_size, MAX_IMAGE_BYTES)

            transcoded = ImageProcessor().process(source, destination)

            self.assertTrue(transcoded)
            self.assertLessEqual(destination.stat().st_size, MAX_IMAGE_BYTES)


if __name__ == "__main__":
    unittest.main()

