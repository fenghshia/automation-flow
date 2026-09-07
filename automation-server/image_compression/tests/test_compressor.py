import importlib.util
import os
import tempfile
import unittest
from pathlib import Path

from image_compression.compressor import ImageCompressionError, ImageProcessor
from image_compression.policy import MAX_IMAGE_BYTES


@unittest.skipUnless(importlib.util.find_spec("PIL"), "Pillow is not installed")
class PillowCompressionTests(unittest.TestCase):
    @staticmethod
    def pad_above_limit(path):
        with Path(path).open("ab") as file:
            file.write(b"\0" * (MAX_IMAGE_BYTES + 1))

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

    def test_large_webp_is_copied_byte_for_byte(self):
        from PIL import Image, features

        if not features.check("webp"):
            self.skipTest("Pillow WebP support is not installed")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.webp"
            destination = root / "destination.webp"
            Image.new("RGB", (16, 16), "red").save(source, format="WEBP")
            self.pad_above_limit(source)
            original = source.read_bytes()

            transcoded = ImageProcessor().process(source, destination)

            self.assertFalse(transcoded)
            self.assertGreater(destination.stat().st_size, MAX_IMAGE_BYTES)
            self.assertEqual(original, destination.read_bytes())

    def test_large_animated_gif_is_copied_byte_for_byte(self):
        from PIL import Image

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.gif"
            destination = root / "destination.gif"
            frames = [
                Image.new("P", (16, 16), color)
                for color in (0, 1)
            ]
            frames[0].save(
                source,
                format="GIF",
                save_all=True,
                append_images=frames[1:],
                duration=10,
                loop=0,
            )
            self.pad_above_limit(source)
            original = source.read_bytes()

            transcoded = ImageProcessor().process(source, destination)

            self.assertFalse(transcoded)
            self.assertGreater(destination.stat().st_size, MAX_IMAGE_BYTES)
            self.assertEqual(original, destination.read_bytes())

    def test_corrupt_large_gif_still_fails_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.gif"
            destination = root / "destination.gif"
            source.write_bytes(b"not-a-gif" + b"\0" * MAX_IMAGE_BYTES)

            with self.assertRaises(ImageCompressionError):
                ImageProcessor().process(source, destination)


if __name__ == "__main__":
    unittest.main()
