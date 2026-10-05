"""Public byte identity helpers for the two media pipelines."""

import hashlib
import os
import stat as stat_module
from pathlib import Path


def reject_media_links(path):
    path = Path(os.path.abspath(path))
    for component in (path, *path.parents):
        try:
            info = component.lstat()
        except FileNotFoundError:
            continue
        if stat_module.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("media_reparse_point_rejected")
    return path


def path_key(path):
    return hashlib.sha256(os.path.normcase(str(Path(path).resolve())).encode("utf-8")).hexdigest()


def snapshot(path):
    path = reject_media_links(path)
    if path.is_symlink():
        raise ValueError("Linked video paths are not managed automatically.")
    stat = path.stat()
    if not path.is_file() or stat.st_size <= 0:
        raise ValueError("A nonempty regular video is required.")
    return {"size_bytes": stat.st_size, "modified_ns": stat.st_mtime_ns,
            "file_identity": {"device": stat.st_dev, "inode": stat.st_ino}}


def hash_stable(path, expected=None):
    before = snapshot(path)
    if expected is not None and before != expected:
        raise ValueError("Source identity changed.")
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    if snapshot(path) != before:
        raise ValueError("Source changed while reading.")
    return digest.hexdigest(), before
