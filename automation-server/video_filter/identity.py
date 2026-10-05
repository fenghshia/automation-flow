"""Stable file identity and content verification, independent of preferences."""

from media_lineage.files import hash_stable, path_key, snapshot

__all__ = ["SUPPORTED_SUFFIXES", "hash_stable", "path_key", "snapshot"]
SUPPORTED_SUFFIXES = {".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v"}

