import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from .policy import archive_suffix


INVALID_CHARACTERS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}
MAX_BASENAME_LENGTH = 240


@dataclass(frozen=True)
class NameCandidate:
    key: str
    provenance: str
    basename: str


def collision_key(name):
    return unicodedata.normalize("NFC", name).casefold()


def _truncate_name(name, max_length=MAX_BASENAME_LENGTH):
    if len(name) <= max_length:
        return name
    suffix = Path(name).suffix
    stem_limit = max(1, max_length - len(suffix))
    return f"{name[:-len(suffix)][:stem_limit]}{suffix}" if suffix else name[:max_length]


def safe_basename(name):
    normalized = unicodedata.normalize("NFC", str(name).replace("\\", "/")).rsplit("/", 1)[-1]
    normalized = INVALID_CHARACTERS.sub("_", normalized).rstrip(" .")
    if not normalized:
        normalized = "_unnamed"
    path = Path(normalized)
    if path.stem.upper() in WINDOWS_RESERVED_NAMES:
        normalized = f"_{normalized}"
    return _truncate_name(normalized)


def prefixed_name(prefix, basename):
    suffix = Path(basename).suffix
    stem = basename[:-len(suffix)] if suffix else basename
    available = max(1, MAX_BASENAME_LENGTH - len(prefix) - len(suffix))
    return f"{prefix}{stem[:available]}{suffix}"


def output_name(source_name, source_kind):
    if source_kind != "archive":
        return safe_basename(source_name)
    suffix = archive_suffix(source_name)
    if suffix is None:
        raise ValueError("Archive source has no supported suffix")
    return safe_basename(source_name[: -len(suffix)])


def allocate_names(candidates):
    prepared = [
        (candidate, safe_basename(candidate.basename))
        for candidate in sorted(
            candidates,
            key=lambda item: (collision_key(item.provenance), item.provenance),
        )
    ]
    reserved = {collision_key(name) for _, name in prepared}
    assigned = set()
    seen_counts = {}
    result = {}

    for candidate, basename in prepared:
        basename_key = collision_key(basename)
        occurrence = seen_counts.get(basename_key, 0)
        seen_counts[basename_key] = occurrence + 1
        if occurrence == 0 and basename_key not in assigned:
            chosen = basename
        else:
            number = 1
            while True:
                chosen = prefixed_name(f"D{number}_", basename)
                chosen_key = collision_key(chosen)
                if chosen_key not in reserved and chosen_key not in assigned:
                    break
                number += 1
        assigned.add(collision_key(chosen))
        result[candidate.key] = chosen
    return result

