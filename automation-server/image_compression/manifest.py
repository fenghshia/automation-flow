import hashlib
import os
import stat
import unicodedata
from dataclasses import dataclass
from pathlib import Path


class UnsafeSourceError(RuntimeError):
    pass


@dataclass(frozen=True)
class SourceSnapshot:
    size_bytes: int
    file_count: int
    modified_ns: int | None
    digest: str


@dataclass(frozen=True)
class ContentManifest:
    digest: str
    file_count: int
    size_bytes: int


def _path_key(path):
    return unicodedata.normalize("NFC", path.as_posix()).casefold()


def _is_link_or_junction(path):
    return path.is_symlink() or (
        hasattr(os.path, "isjunction") and os.path.isjunction(path)
    )


def iter_regular_files(root):
    root = Path(root)
    if _is_link_or_junction(root):
        raise UnsafeSourceError(f"Links are not supported: {root.name}")
    if root.is_file():
        yield Path("."), root
        return
    if not root.is_dir():
        raise UnsafeSourceError(f"Source is not a regular file or directory: {root.name}")

    files = []
    for current, directories, filenames in os.walk(root, followlinks=False):
        current_path = Path(current)
        for directory in list(directories):
            candidate = current_path / directory
            if _is_link_or_junction(candidate):
                raise UnsafeSourceError(f"Links are not supported: {candidate.relative_to(root)}")
        for filename in filenames:
            candidate = current_path / filename
            if _is_link_or_junction(candidate):
                raise UnsafeSourceError(f"Links are not supported: {candidate.relative_to(root)}")
            mode = candidate.stat(follow_symlinks=False).st_mode
            if not stat.S_ISREG(mode):
                raise UnsafeSourceError(f"Special files are not supported: {candidate.relative_to(root)}")
            files.append((candidate.relative_to(root), candidate))
    yield from sorted(files, key=lambda item: (_path_key(item[0]), item[0].as_posix()))


def source_snapshot(path):
    path = Path(path)
    digest = hashlib.sha256()
    size_bytes = 0
    file_count = 0
    modified_ns = None
    if path.is_file() and not _is_link_or_junction(path):
        stat_result = path.stat()
        modified_ns = stat_result.st_mtime_ns

    for relative, file_path in iter_regular_files(path):
        stat_result = file_path.stat(follow_symlinks=False)
        size_bytes += stat_result.st_size
        file_count += 1
        record = (
            f"{_path_key(relative)}\0{stat_result.st_size}\0{stat_result.st_mtime_ns}\n"
        )
        digest.update(record.encode("utf-8"))
    return SourceSnapshot(size_bytes, file_count, modified_ns, digest.hexdigest())


def file_sha256(path, chunk_size=1024 * 1024):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        while chunk := source.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def content_manifest(path):
    path = Path(path)
    digest = hashlib.sha256()
    total_size = 0
    count = 0
    for relative, file_path in iter_regular_files(path):
        size = file_path.stat().st_size
        content_hash = file_sha256(file_path)
        digest.update(
            f"{_path_key(relative)}\0{size}\0{content_hash}\n".encode("utf-8")
        )
        total_size += size
        count += 1
    return ContentManifest(digest.hexdigest(), count, total_size)
