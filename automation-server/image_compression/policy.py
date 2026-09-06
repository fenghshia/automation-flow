MAX_IMAGE_BYTES = 3 * 1024 * 1024
MAX_ARCHIVE_DEPTH = 16
MAX_ARCHIVE_MEMBERS = 100_000
MAX_EXPANDED_BYTES = 500 * 1024**3
MAX_COMPRESSION_RATIO = 1_000
MIN_FREE_BYTES = 1024**3


ARCHIVE_SUFFIXES = tuple(
    sorted(
        {
            ".tar.bz2",
            ".tar.gz",
            ".tar.xz",
            ".tar.zst",
            ".tbz2",
            ".tgz",
            ".txz",
            ".tzst",
            ".7z",
            ".bz2",
            ".gz",
            ".rar",
            ".tar",
            ".xz",
            ".zip",
            ".zst",
        },
        key=len,
        reverse=True,
    )
)

KNOWN_IMAGE_EXTENSIONS = {
    ".apng",
    ".avif",
    ".bmp",
    ".gif",
    ".heic",
    ".heif",
    ".jpe",
    ".jpeg",
    ".jpg",
    ".png",
    ".tif",
    ".tiff",
    ".webp",
}

FORMAT_EXTENSIONS = {
    "AVIF": {".avif"},
    "BMP": {".bmp"},
    "GIF": {".gif"},
    "HEIF": {".heic", ".heif"},
    "JPEG": {".jpe", ".jpeg", ".jpg"},
    "PNG": {".apng", ".png"},
    "TIFF": {".tif", ".tiff"},
    "WEBP": {".webp"},
}

REENCODABLE_FORMATS = {"JPEG", "PNG", "WEBP"}


def archive_suffix(name):
    lower_name = name.casefold()
    return next((suffix for suffix in ARCHIVE_SUFFIXES if lower_name.endswith(suffix)), None)


def is_archive_name(name):
    return archive_suffix(name) is not None

