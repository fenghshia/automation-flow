import logging
import math
import shutil
import uuid
import warnings
from dataclasses import dataclass
from pathlib import Path

from .manifest import file_sha256
from .policy import (
    CANONICAL_FORMAT_EXTENSIONS,
    FORMAT_EXTENSIONS,
    KNOWN_IMAGE_EXTENSIONS,
    MAX_DECODE_PIXELS,
    MAX_IMAGE_BYTES,
    PASSTHROUGH_FORMATS,
    REENCODABLE_FORMATS,
)


class ImageCompressionError(RuntimeError):
    error_code = "IMAGE_PROCESSING_FAILED"


class ImageDecodeError(ImageCompressionError):
    error_code = "IMAGE_DECODE_FAILED"


class ImageDecodePixelLimitError(ImageCompressionError):
    error_code = "IMAGE_DECODE_PIXEL_LIMIT"


class ImageFormatUnmappedError(ImageCompressionError):
    error_code = "IMAGE_FORMAT_UNMAPPED"


class ImageOutputFormatError(ImageCompressionError):
    error_code = "IMAGE_PROCESSING_FAILED"


class UnsupportedImageError(ImageCompressionError):
    pass


@dataclass(frozen=True)
class ImageInspection:
    image_format: str
    original_suffix: str
    canonical_suffix: str
    width: int
    height: int
    pixel_count: int
    animated: bool
    source_size_bytes: int
    extension_matches: bool
    pixel_warning: bool


def _load_pillow():
    try:
        from PIL import Image, ImageOps, ImageSequence, UnidentifiedImageError
    except ImportError as error:
        raise ImageCompressionError(
            "Pillow is required. Install it with: "
            "mamba run -n autoflow python -m pip install Pillow"
        ) from error
    return Image, ImageOps, ImageSequence, UnidentifiedImageError


def _load_pyvips():
    try:
        import pyvips
    except (ImportError, OSError) as error:
        raise ImageCompressionError(
            "pyvips and libvips are required. Install them with: "
            "conda install -n autoflow --override-channels "
            "-c conda-forge pyvips libvips"
        ) from error
    pyvips_logger = logging.getLogger("pyvips")
    if pyvips_logger.level == logging.NOTSET:
        pyvips_logger.setLevel(logging.WARNING)
    return pyvips


def copy_verified(source, destination):
    source = Path(source)
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
    if source.stat().st_size != destination.stat().st_size:
        raise ImageCompressionError("Copied file size does not match the source")
    if file_sha256(source) != file_sha256(destination):
        raise ImageCompressionError("Copied file hash does not match the source")


class VipsCompressionBackend:
    JPEG_MIN_QUALITY = 35
    JPEG_MAX_QUALITY = 95
    PNG_PALETTE_QUALITIES = (90, 70, 50)
    MAX_RESIZE_ATTEMPTS = 24

    @staticmethod
    def validate_dependency():
        _load_pyvips()

    @staticmethod
    def _working_edge(inspection):
        longest_edge = max(inspection.width, inspection.height)
        if inspection.pixel_count <= MAX_DECODE_PIXELS:
            return longest_edge
        scale = math.sqrt(MAX_DECODE_PIXELS / inspection.pixel_count)
        return max(1, int(longest_edge * scale))

    @staticmethod
    def _save_jpeg(image, path, quality):
        image.jpegsave(
            str(path),
            Q=quality,
            optimize_coding=True,
            interlace=True,
            keep=_load_pyvips().enums.ForeignKeep.ICC,
        )

    @staticmethod
    def _save_png(image, path, *, palette=False, quality=100):
        image.pngsave(
            str(path),
            compression=9,
            palette=palette,
            Q=quality,
            effort=7,
            keep=_load_pyvips().enums.ForeignKeep.ICC,
        )

    @staticmethod
    def _candidate_size(path):
        return Path(path).stat().st_size

    @staticmethod
    def _load_working_image(source, working_edge):
        return _load_pyvips().Image.thumbnail(
            str(source),
            working_edge,
            height=working_edge,
            size="down",
            fail_on="error",
        )

    def _encode_jpeg(self, source, working_edge, candidate):
        low = self.JPEG_MIN_QUALITY
        high = self.JPEG_MAX_QUALITY
        best_quality = None
        smallest_size = None
        dimensions = None
        while low <= high:
            quality = (low + high) // 2
            image = self._load_working_image(source, working_edge)
            dimensions = (image.width, image.height)
            self._save_jpeg(image, candidate, quality)
            size = self._candidate_size(candidate)
            smallest_size = size if smallest_size is None else min(smallest_size, size)
            if size <= MAX_IMAGE_BYTES:
                best_quality = quality
                low = quality + 1
            else:
                high = quality - 1
        if best_quality is None:
            return False, smallest_size, dimensions
        image = self._load_working_image(source, working_edge)
        self._save_jpeg(image, candidate, best_quality)
        return True, self._candidate_size(candidate), (image.width, image.height)

    def _encode_png(self, source, working_edge, candidate):
        smallest_size = None
        image = self._load_working_image(source, working_edge)
        dimensions = (image.width, image.height)
        self._save_png(image, candidate)
        size = self._candidate_size(candidate)
        smallest_size = size
        if size <= MAX_IMAGE_BYTES:
            return True, size, dimensions

        pyvips = _load_pyvips()
        if not pyvips.type_find("VipsOperation", "quantise"):
            return False, smallest_size, dimensions
        for quality in self.PNG_PALETTE_QUALITIES:
            try:
                image = self._load_working_image(source, working_edge)
                self._save_png(image, candidate, palette=True, quality=quality)
            except pyvips.Error:
                continue
            size = self._candidate_size(candidate)
            smallest_size = min(smallest_size, size)
            if size <= MAX_IMAGE_BYTES:
                return True, size, dimensions
        return False, smallest_size, dimensions

    def compress(self, source, candidate, inspection):
        source = Path(source)
        candidate = Path(candidate)
        image_format = inspection.image_format
        pyvips = _load_pyvips()
        try:
            working_edge = self._working_edge(inspection)
            for _ in range(self.MAX_RESIZE_ATTEMPTS):
                if candidate.exists():
                    candidate.unlink()
                if image_format == "JPEG":
                    accepted, smallest_size, dimensions = self._encode_jpeg(
                        source, working_edge, candidate
                    )
                else:
                    accepted, smallest_size, dimensions = self._encode_png(
                        source, working_edge, candidate
                    )
                if accepted:
                    return
                if min(dimensions) <= 1:
                    break
                ratio = math.sqrt(MAX_IMAGE_BYTES / max(smallest_size, 1)) * 0.95
                ratio = min(0.9, max(0.1, ratio))
                next_edge = max(1, int(working_edge * ratio))
                working_edge = (
                    next_edge if next_edge < working_edge else max(1, working_edge - 1)
                )
            raise ImageCompressionError("Unable to compress image below 3 MiB")
        except ImageCompressionError:
            raise
        except (OSError, pyvips.Error) as error:
            raise ImageCompressionError(
                f"libvips failed to compress image: {source}"
            ) from error


class ImageProcessor:
    def __init__(self, compression_backend=None):
        self.compression_backend = compression_backend or VipsCompressionBackend()

    def validate_dependency(self):
        _load_pillow()
        if hasattr(self.compression_backend, "validate_dependency"):
            self.compression_backend.validate_dependency()

    @staticmethod
    def _format_matches_extension(image_format, suffix):
        return suffix.casefold() in FORMAT_EXTENSIONS.get(image_format, set())

    @staticmethod
    def _is_animated(image):
        return bool(getattr(image, "is_animated", False) and getattr(image, "n_frames", 1) > 1)

    def probe(self, path):
        path = Path(path)
        Image, _, _, UnidentifiedImageError = _load_pillow()
        try:
            with warnings.catch_warnings(record=True) as caught_warnings:
                warnings.simplefilter("always", Image.DecompressionBombWarning)
                with Image.open(path) as image:
                    image_format = (image.format or "").upper()
                    canonical_suffix = CANONICAL_FORMAT_EXTENSIONS.get(image_format)
                    if canonical_suffix is None:
                        raise ImageFormatUnmappedError(
                            f"Image format has no canonical extension mapping: "
                            f"{image_format or 'unknown'} ({path})"
                        )
                    width, height = image.size
                    animated = self._is_animated(image)
                    image.verify()
                pixel_count = width * height
                return ImageInspection(
                    image_format=image_format,
                    original_suffix=path.suffix,
                    canonical_suffix=canonical_suffix,
                    width=width,
                    height=height,
                    pixel_count=pixel_count,
                    animated=animated,
                    source_size_bytes=path.stat().st_size,
                    extension_matches=self._format_matches_extension(
                        image_format, path.suffix
                    ),
                    pixel_warning=(
                        pixel_count > MAX_DECODE_PIXELS
                        or any(
                            issubclass(item.category, Image.DecompressionBombWarning)
                            for item in caught_warnings
                        )
                    ),
                )
        except Image.DecompressionBombError as error:
            raise ImageDecodePixelLimitError(
                f"Image exceeds Pillow's hard pixel limit: {path}"
            ) from error
        except UnidentifiedImageError as error:
            if path.suffix.casefold() not in KNOWN_IMAGE_EXTENSIONS:
                return None
            raise ImageDecodeError(f"Image cannot be decoded: {path}") from error
        except OSError as error:
            raise ImageDecodeError(f"Image cannot be verified: {path}") from error

    # Retained as a compatibility alias for callers that used the old name.
    def inspect(self, path):
        return self.probe(path)

    def validate_decodable(self, path, inspection):
        path = Path(path)
        if inspection.pixel_count > MAX_DECODE_PIXELS:
            raise ImageDecodePixelLimitError(
                f"Image requires full decode but exceeds pixel limit: path={path} | "
                f"width={inspection.width} | height={inspection.height} | "
                f"pixels={inspection.pixel_count} | limit={MAX_DECODE_PIXELS}"
            )
        Image, _, _, UnidentifiedImageError = _load_pillow()
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(path) as image:
                    if inspection.animated:
                        for frame in range(image.n_frames):
                            image.seek(frame)
                            image.load()
                    else:
                        image.load()
        except (UnidentifiedImageError, OSError) as error:
            raise ImageDecodeError(f"Image cannot be fully decoded: {path}") from error
        return inspection

    def verify(self, path, require_limit, full_decode=False):
        inspected = self.probe(path)
        if inspected is None:
            raise ImageCompressionError(f"Output is not a supported image type: {Path(path).name}")
        if not inspected.extension_matches:
            raise ImageOutputFormatError(
                f"Output format does not match its extension: path={Path(path)} | "
                f"format={inspected.image_format} | suffix={inspected.original_suffix or '<none>'}"
            )
        if require_limit and Path(path).stat().st_size > MAX_IMAGE_BYTES:
            raise ImageCompressionError("Compressed image is still above 3 MiB")
        if full_decode:
            self.validate_decodable(path, inspected)
        return inspected

    def process(self, source, destination, inspection=None):
        source = Path(source)
        destination = Path(destination)
        inspection = self.probe(source) if inspection is None else inspection
        if inspection is None:
            copy_verified(source, destination)
            return False

        image_format = inspection.image_format
        animated = inspection.animated
        if image_format in PASSTHROUGH_FORMATS:
            copy_verified(source, destination)
            self.verify(destination, require_limit=False)
            return False
        if source.stat().st_size <= MAX_IMAGE_BYTES:
            copy_verified(source, destination)
            self.verify(destination, require_limit=True)
            return False
        if animated or image_format not in REENCODABLE_FORMATS:
            raise UnsupportedImageError(
                f"Images of format {image_format or 'unknown'} above 3 MiB are not supported"
            )

        destination.parent.mkdir(parents=True, exist_ok=True)
        candidate = destination.with_name(
            f".{destination.stem}.{uuid.uuid4().hex}{destination.suffix}"
        )
        try:
            self.compression_backend.compress(source, candidate, inspection)
            self.verify(candidate, require_limit=True, full_decode=True)
            candidate.replace(destination)
        finally:
            candidate.unlink(missing_ok=True)
        return True
