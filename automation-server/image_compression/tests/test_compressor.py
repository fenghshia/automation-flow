import importlib.util
import os
import stat
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock, patch

from image_compression.compressor import (
    ImageCompressionError,
    ImageInspection,
    ImageOutputFormatError,
    ImageProcessor,
    copy_verified,
)
from image_compression.policy import MAX_DECODE_PIXELS, MAX_IMAGE_BYTES


@unittest.skipUnless(os.name == "nt", "Windows read-only semantics are required")
class WindowsCopyTests(unittest.TestCase):
    def test_copy_does_not_propagate_readonly_attribute(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.png"
            destination = root / "destination.png"
            source.write_bytes(b"image")
            source.chmod(stat.S_IREAD)

            try:
                copy_verified(source, destination)
            finally:
                source.chmod(stat.S_IWRITE)

            self.assertFalse(
                destination.stat().st_file_attributes & stat.FILE_ATTRIBUTE_READONLY
            )


@unittest.skipUnless(
    importlib.util.find_spec("PIL") and importlib.util.find_spec("pyvips"),
    "Pillow and pyvips are required",
)
class ImageCompressionTests(unittest.TestCase):
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

    def test_mislabeled_jpeg_is_copied_to_canonical_extension(self):
        from PIL import Image

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.png"
            destination = root / "source.jpg"
            Image.new("RGB", (16, 16), "red").save(source, format="JPEG")
            original = source.read_bytes()
            processor = ImageProcessor()

            inspection = processor.probe(source)
            transcoded = processor.process(source, destination, inspection)

            self.assertEqual("JPEG", inspection.image_format)
            self.assertFalse(inspection.extension_matches)
            self.assertEqual(".jpg", inspection.canonical_suffix)
            self.assertFalse(transcoded)
            self.assertEqual(original, destination.read_bytes())

    def test_valid_jpeg_alias_is_preserved(self):
        from PIL import Image

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "source.jpeg"
            Image.new("RGB", (8, 8), "red").save(path, format="JPEG")

            inspection = ImageProcessor().probe(path)

            self.assertTrue(inspection.extension_matches)
            self.assertEqual(".jpeg", inspection.original_suffix)

    def test_mpo_named_jpg_is_copied_to_mpo_byte_for_byte(self):
        from PIL import Image

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.jpg"
            destination = root / "source.mpo"
            frames = [
                Image.new("RGB", (16, 16), color)
                for color in ("red", "blue")
            ]
            frames[0].save(
                source,
                format="MPO",
                save_all=True,
                append_images=frames[1:],
            )
            self.pad_above_limit(source)
            original = source.read_bytes()
            backend = MagicMock()
            backend.compress.side_effect = AssertionError("MPO must not be compressed")
            processor = ImageProcessor(compression_backend=backend)

            inspection = processor.probe(source)
            transcoded = processor.process(source, destination, inspection)

            self.assertEqual("MPO", inspection.image_format)
            self.assertEqual(".mpo", inspection.canonical_suffix)
            self.assertFalse(inspection.extension_matches)
            self.assertFalse(transcoded)
            self.assertEqual(original, destination.read_bytes())
            backend.compress.assert_not_called()

    def test_output_with_mismatched_extension_is_rejected(self):
        from PIL import Image

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "output.png"
            Image.new("RGB", (8, 8), "red").save(path, format="JPEG")

            with self.assertRaises(ImageOutputFormatError):
                ImageProcessor().verify(path, require_limit=True)

    def test_copy_path_does_not_request_full_decode(self):
        from PIL import Image

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.jpg"
            destination = root / "destination.jpg"
            Image.new("RGB", (8, 8), "red").save(source, format="JPEG")
            processor = ImageProcessor()
            inspection = processor.probe(source)
            inspection = replace(
                inspection,
                width=MAX_DECODE_PIXELS + 1,
                pixel_count=MAX_DECODE_PIXELS + 1,
                pixel_warning=True,
            )

            with patch.object(
                processor,
                "validate_decodable",
                side_effect=AssertionError("full decode must not be requested"),
            ):
                processor.process(source, destination, inspection)

            self.assertEqual(source.read_bytes(), destination.read_bytes())

    def test_vips_working_edge_reduces_pixel_count_above_decode_limit(self):
        inspection = ImageInspection(
            image_format="JPEG",
            original_suffix=".jpg",
            canonical_suffix=".jpg",
            width=12_500,
            height=12_499,
            pixel_count=156_237_500,
            animated=False,
            source_size_bytes=MAX_IMAGE_BYTES + 1,
            extension_matches=True,
            pixel_warning=True,
        )

        edge = ImageProcessor().compression_backend._working_edge(inspection)

        self.assertLess(edge, max(inspection.width, inspection.height))
        self.assertLessEqual(edge * edge, MAX_DECODE_PIXELS)

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

    def test_vips_jpeg_applies_exif_orientation_and_preserves_icc(self):
        from PIL import Image, ImageCms

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.jpg"
            destination = root / "destination.jpg"
            exif = Image.Exif()
            exif[274] = 6
            icc_profile = ImageCms.ImageCmsProfile(
                ImageCms.createProfile("sRGB")
            ).tobytes()
            Image.new("RGB", (40, 20), "red").save(
                source,
                format="JPEG",
                exif=exif,
                icc_profile=icc_profile,
            )
            self.pad_above_limit(source)

            ImageProcessor().process(source, destination)

            with Image.open(destination) as output:
                self.assertEqual((20, 40), output.size)
                self.assertNotEqual(6, output.getexif().get(274))
                self.assertTrue(output.info.get("icc_profile"))

    def test_large_rgba_png_is_reduced_below_limit_with_alpha(self):
        from PIL import Image

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.png"
            destination = root / "destination.png"
            image = Image.frombytes("RGBA", (1024, 1024), os.urandom(1024 * 1024 * 4))
            image.save(source, compress_level=0)
            self.assertGreater(source.stat().st_size, MAX_IMAGE_BYTES)

            transcoded = ImageProcessor().process(source, destination)

            self.assertTrue(transcoded)
            self.assertLessEqual(destination.stat().st_size, MAX_IMAGE_BYTES)
            with Image.open(destination) as output:
                self.assertEqual("PNG", output.format)
                self.assertIn("A", output.convert("RGBA").getbands())

    def test_backend_failure_removes_partial_candidate(self):
        from PIL import Image

        class FailingBackend:
            @staticmethod
            def compress(source, candidate, inspection):
                Path(candidate).write_bytes(b"partial")
                raise ImageCompressionError("failed")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.jpg"
            destination = root / "destination.jpg"
            Image.new("RGB", (16, 16), "red").save(source, format="JPEG")
            self.pad_above_limit(source)

            with self.assertRaises(ImageCompressionError):
                ImageProcessor(compression_backend=FailingBackend()).process(
                    source, destination
                )

            self.assertFalse(destination.exists())
            self.assertEqual([], list(root.glob(".*.jpg")))

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
