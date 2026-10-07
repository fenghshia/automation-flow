"""Pure, read-only image group configuration and scope validation."""

import hashlib
import json
import os
import re
import stat
import unicodedata
from pathlib import Path


PENDING_ROOT = Path(__file__).with_name("pending")
GROUP_NAME = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")


def reject_links(path):
    path = Path(path).absolute()
    for candidate in (path, *path.parents):
        try:
            info = candidate.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or (
            getattr(info, "st_file_attributes", 0)
            & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        ):
            raise ValueError("image_directory_links_not_supported")
    return path


def anchored(value, base):
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ValueError("image_directory_required")
    path = Path(value).expanduser()
    return reject_links(path if path.is_absolute() else Path(base) / path).resolve()


def directory_key(path):
    # No filesystem reads: also usable for historical DB paths during polling.
    value = unicodedata.normalize("NFC", os.path.normpath(str(Path(path).absolute())))
    return hashlib.sha256(value.casefold().encode("utf-8")).hexdigest()


def validate_groups(groups, pending_root=PENDING_ROOT):
    roots = [reject_links(pending_root).resolve()]
    for group in groups:
        for role in ("source_directory", "output_directory"):
            path = reject_links(group[role]).resolve()
            if path.exists() and not path.is_dir():
                raise ValueError("image_root_must_be_directory")
            roots.append(path)
    for index, first in enumerate(roots):
        for second in roots[index + 1:]:
            a, b = directory_key(first), directory_key(second)
            if (a == b or a in {directory_key(p) for p in second.parents}
                    or b in {directory_key(p) for p in first.parents}
                    or (first.exists() and second.exists() and first.samefile(second))):
                raise ValueError("image_group_directories_overlap")
    return groups


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_image_configuration_key")
        result[key] = value
    return result


def load_groups(path, base, pending_root=PENDING_ROOT):
    path = reject_links(path)
    if path.stat().st_size > 1024 * 1024:
        raise ValueError("image_configuration_too_large")
    data = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_object)
    if (not isinstance(data, dict) or set(data) != {"schema_version", "groups"}
            or type(data["schema_version"]) is not int or data["schema_version"] != 1
            or not isinstance(data["groups"], dict) or not 1 <= len(data["groups"]) <= 100):
        raise ValueError("invalid_image_group_configuration")
    groups, names = [], set()
    for name, values in data["groups"].items():
        if (not GROUP_NAME.fullmatch(name) or name.casefold() in names
                or name.casefold() == "legacy"):
            raise ValueError("invalid_image_group_name")
        names.add(name.casefold())
        if (not isinstance(values, dict) or set(values) != {"SOURCE_DIR", "OUTPUT_DIR", "FLATTEN"}
                or type(values["FLATTEN"]) is not bool):
            raise ValueError("three_image_group_fields_required")
        source = anchored(values["SOURCE_DIR"], base)
        output = anchored(values["OUTPUT_DIR"], base)
        groups.append({"name": name, "source_directory": source,
                       "output_directory": output, "flatten": values["FLATTEN"],
                       "output_scope_key": directory_key(output)})
    return validate_groups(groups, pending_root)


def mission_matches(mission, group):
    return bool(
        mission.group_name == group["name"]
        and mission.source_directory and mission.output_directory
        and directory_key(mission.source_directory) == directory_key(group["source_directory"])
        and directory_key(mission.output_directory) == group["output_scope_key"]
        and mission.output_scope_key == group["output_scope_key"]
        and mission.flatten is group["flatten"]
        and directory_key(Path(mission.source_path).parent) == directory_key(group["source_directory"])
        and (not mission.output_path or directory_key(Path(mission.output_path).parent)
             == group["output_scope_key"])
    )
