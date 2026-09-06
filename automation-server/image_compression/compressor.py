import math
import shutil
import warnings
from io import BytesIO
from pathlib import Path

from .manifest import file_sha256
from .policy import (
    FORMAT_EXTENSIONS,
    KNOWN_IMAGE_EXTENSIONS,
    MAX_IMAGE_BYTES,
    REENCODABLE_FORMATS,
)


class ImageCompressionError(RuntimeError):
    pass


class UnsupportedImageError(ImageCompressionError):
    pass


def _load_pillow():
    try:
        from PIL import Image, ImageOps, ImageSequence, UnidentifiedImageError
    except ImportError as error:
        raise ImageCompressionError(
            "Pillow is required. Install it with: "
            "mamba run -n autoflow python -m pip install Pillow"
        ) from error
    return Image, ImageOps, ImageSequence, UnidentifiedImageError


def copy_verified(source, destination):
    source = Path(source)
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    if source.stat().st_size != destination.stat().st_size:
        raise ImageCompressionError("Copied file size does not match the source")
    if file_sha256(source) != file_sha256(destination):
        raise ImageCompressionError("Copied file hash does not match the source")


class ImageProcessor:
    @staticmethod
    def validate_dependency():
        _load_pillow()

    @staticmethod
    def _format_matches_extension(image_format, suffix):
        return suffix.casefold() in FORMAT_EXTENSIONS.get(image_format, set())

    @staticmethod
    def _is_animated(image):
        return bool(getattr(image, "is_animated", False) and getattr(image, "n_frames", 1) > 1)

    def inspect(self, path):
        path = Path(path)
        Image, _, _, UnidentifiedImageError = _load_pillow()
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(path) as image:
                    image_format = (image.format or "").upper()
                    if not self._format_matches_extension(image_format, path.suffix):
                        raise UnsupportedImageError(
                            f"Image format does not match its extension: {path.name}"
                        )
                    animated = self._is_animated(image)
                    image.verify()
                with Image.open(path) as image:
                    if animated:
                        for frame in range(image.n_frames):
                            image.seek(frame)
                            image.load()
                    else:
                        image.load()
                    return image_format, animated
        except UnidentifiedImageError as error:
            if path.suffix.casefold() not in KNOWN_IMAGE_EXTENSIONS:
                return None
            raise ImageCompressionError(f"Image cannot be decoded: {path.name}") from error

    def verify(self, path, require_limit):
        inspected = self.inspect(path)
        if inspected is None:
            raise ImageCompressionError(f"Output is not a supported image type: {Path(path).name}")
        if require_limit and Path(path).stat().st_size > MAX_IMAGE_BYTES:
            raise ImageCompressionError("Compressed image is still above 3 MiB")
        return inspected

    @staticmethod
    def _save_bytes(image, image_format, **options):
        output = BytesIO()
        image.save(output, format=image_format, **options)
        return output.getvalue()

    def _quality_search(self, image, image_format, minimum, metadata):
        low = minimum
        high = 95
        best = None
        smallest = None
        while low <= high:
            quality = (low + high) // 2
            options = dict(metadata)
            options["quality"] = quality
            if image_format == "JPEG":
                options.update(optimize=True, progressive=True)
            else:
                options["method"] = 6
            encoded = self._save_bytes(image, image_format, **options)
            if smallest is None or len(encoded) < len(smallest):
                smallest = encoded
            if len(encoded) <= MAX_IMAGE_BYTES:
                best = encoded
                low = quality + 1
            else:
                high = quality - 1
        return best, smallest

    def _png_candidates(self, image, metadata):
        candidates = [
            self._save_bytes(image, "PNG", optimize=True, compress_level=9, **metadata)
        ]
        quantize_source = image.convert("RGBA") if image.mode == "LA" else image
        quantize_method = 2 if quantize_source.mode == "RGBA" else 0
        for colors in (256, 128, 64):
            quantized = quantize_source.quantize(colors=colors, method=quantize_method)
            candidates.append(
                self._save_bytes(
                    quantized, "PNG", optimize=True, compress_level=9, **metadata
                )
            )
        acceptable = [candidate for candidate in candidates if len(candidate) <= MAX_IMAGE_BYTES]
        return (acceptable[0] if acceptable else None), min(candidates, key=len)

    def _compress(self, source, image_format):
        Image, ImageOps, _, _ = _load_pillow()
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(source) as opened:
                image = ImageOps.exif_transpose(opened)
                image.load()
                if image_format == "JPEG" and image.mode not in {"L", "RGB", "CMYK"}:
                    image = image.convert("RGB")
                metadata = {}
                if opened.info.get("icc_profile"):
                    metadata["icc_profile"] = opened.info["icc_profile"]

        current = image
        for _ in range(24):
            if image_format == "PNG":
                best, smallest = self._png_candidates(current, metadata)
            else:
                minimum = 35 if image_format == "JPEG" else 30
                best, smallest = self._quality_search(
                    current, image_format, minimum, metadata
                )
            if best is not None:
                return best
            if current.width <= 1 and current.height <= 1:
                break
            ratio = math.sqrt(MAX_IMAGE_BYTES / max(len(smallest), 1)) * 0.95
            ratio = min(0.9, max(0.1, ratio))
            size = (
                max(1, int(current.width * ratio)),
                max(1, int(current.height * ratio)),
            )
            if size == current.size:
                size = (max(1, current.width - 1), max(1, current.height - 1))
            current = current.resize(size, Image.Resampling.LANCZOS)
        raise ImageCompressionError("Unable to compress image below 3 MiB")

    def process(self, source, destination):
        source = Path(source)
        destination = Path(destination)
        inspected = self.inspect(source)
        if inspected is None:
            copy_verified(source, destination)
            return False

        image_format, animated = inspected
        if source.stat().st_size <= MAX_IMAGE_BYTES:
            copy_verified(source, destination)
            self.verify(destination, require_limit=True)
            return False
        if animated or image_format not in REENCODABLE_FORMATS:
            raise UnsupportedImageError(
                f"Images of format {image_format or 'unknown'} above 3 MiB are not supported"
            )

        encoded = self._compress(source, image_format)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(encoded)
        self.verify(destination, require_limit=True)
        return True
