"""Stable file identity and content verification, independent of preferences."""

from media_lineage.files import hash_stable, path_key, snapshot

__all__ = ["SUPPORTED_SUFFIXES", "hash_stable", "path_key", "snapshot", "SourceMissing", "source_snapshot", "source_hash"]
SUPPORTED_SUFFIXES = {".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v"}


class SourceMissing(FileNotFoundError):
    """A managed video vanished; missing model artifacts remain real errors."""


def source_snapshot(path):
    try:
        return snapshot(path)
    except FileNotFoundError as error:
        raise SourceMissing("source_missing") from error


def source_hash(path, expected=None):
    try:
        return hash_stable(path, expected)
    except FileNotFoundError as error:
        raise SourceMissing("source_missing") from error
    except ValueError as error:
        if str(error) in ("Source identity changed.", "Source changed while reading."):
            raise ValueError("source_version_changed") from error
        raise
