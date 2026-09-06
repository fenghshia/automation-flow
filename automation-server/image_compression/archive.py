import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .manifest import UnsafeSourceError, iter_regular_files
from .policy import (
    MAX_ARCHIVE_DEPTH,
    MAX_ARCHIVE_MEMBERS,
    MAX_COMPRESSION_RATIO,
    MAX_EXPANDED_BYTES,
    MIN_FREE_BYTES,
)


class ArchiveError(RuntimeError):
    pass


class ArchiveLimitError(ArchiveError):
    pass


class UnsafeArchiveEntryError(ArchiveError):
    pass


@dataclass
class ArchiveBudget:
    members: int = 0
    expanded_bytes: int = 0

    def add(self, members, expanded_bytes, packed_bytes):
        self.members += members
        self.expanded_bytes += expanded_bytes
        if self.members > MAX_ARCHIVE_MEMBERS:
            raise ArchiveLimitError("Archive member count exceeds the configured limit")
        if self.expanded_bytes > MAX_EXPANDED_BYTES:
            raise ArchiveLimitError("Expanded archive size exceeds the configured limit")
        if packed_bytes > 0 and expanded_bytes / packed_bytes > MAX_COMPRESSION_RATIO:
            raise ArchiveLimitError("Archive compression ratio exceeds the configured limit")


class ArchiveExtractor:
    def __init__(self, seven_zip_bin_directory):
        self.executable = Path(seven_zip_bin_directory) / "7z.exe"
        if not self.executable.is_file():
            raise ArchiveError("Required executable does not exist: 7z.exe")

    @staticmethod
    def _decode(output):
        return (output or b"").decode("utf-8", errors="replace")

    def _run(self, arguments, timeout):
        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            completed = subprocess.run(
                [str(self.executable), *map(str, arguments)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=timeout,
                creationflags=creation_flags,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise ArchiveError("7-Zip could not complete the requested operation") from error
        if completed.returncode != 0:
            detail = self._decode(completed.stderr or completed.stdout).strip()[-2000:]
            raise ArchiveError(f"7-Zip failed: {detail or 'no diagnostic output'}")
        return self._decode(completed.stdout)

    @staticmethod
    def _parse_records(output):
        records = []
        current = {}
        for line in output.splitlines():
            if not line.strip():
                if current:
                    records.append(current)
                    current = {}
                continue
            if " = " in line:
                key, value = line.split(" = ", 1)
                current[key.strip()] = value.strip()
        if current:
            records.append(current)
        return records

    @staticmethod
    def _validate_member_name(name):
        normalized = name.replace("\\", "/")
        path = PurePosixPath(normalized)
        if (
            not normalized
            or normalized.startswith(("/", "//"))
            or re.match(r"^[A-Za-z]:", normalized)
            or ".." in path.parts
        ):
            raise UnsafeArchiveEntryError("Archive contains an unsafe member path")

    def inspect(self, archive_path, budget):
        archive_path = Path(archive_path)
        output = self._run(
            ["l", "-slt", "-ba", "-sccUTF-8", "--", archive_path], timeout=120
        )
        members = 0
        expanded_bytes = 0
        for record in self._parse_records(output):
            if "Type" in record and "Folder" not in record:
                continue
            name = record.get("Path")
            if not name:
                continue
            self._validate_member_name(name)
            if record.get("Encrypted") == "+":
                raise ArchiveError("Encrypted archives are not supported")
            if record.get("Symbolic Link") or record.get("Hard Link"):
                raise UnsafeArchiveEntryError("Archive links are not supported")
            is_directory = record.get("Folder") == "+" or record.get("Attributes", "").startswith("D")
            if not is_directory:
                members += 1
                try:
                    expanded_bytes += int(record.get("Size", "0"))
                except ValueError as error:
                    raise ArchiveError("7-Zip returned an invalid member size") from error
        budget.add(members, expanded_bytes, max(archive_path.stat().st_size, 1))
        return members, expanded_bytes

    def extract(self, archive_path, destination, depth, budget):
        if depth > MAX_ARCHIVE_DEPTH:
            raise ArchiveLimitError("Nested archive depth exceeds the configured limit")
        destination = Path(destination)
        if destination.exists():
            raise ArchiveError("Archive extraction destination already exists")
        _, expanded_bytes = self.inspect(archive_path, budget)
        usage = shutil.disk_usage(destination.parent)
        reserve = max(MIN_FREE_BYTES, int(usage.total * 0.05))
        if expanded_bytes > max(0, usage.free - reserve):
            raise ArchiveLimitError("There is not enough free space to extract the archive safely")
        self._run(["t", "-sccUTF-8", "--", archive_path], timeout=120)
        destination.mkdir(parents=True)
        timeout = max(300, min(7200, int(expanded_bytes / (20 * 1024**2)) + 300))
        try:
            self._run(
                ["x", "-y", "-aoa", "-sccUTF-8", f"-o{destination}", "--", archive_path],
                timeout=timeout,
            )
            root = destination.resolve()
            extracted = []
            for relative, path in iter_regular_files(destination):
                try:
                    path.resolve().relative_to(root)
                except ValueError as error:
                    raise UnsafeArchiveEntryError(
                        "Archive extraction escaped the task directory"
                    ) from error
                extracted.append((relative, path))
            return extracted
        except (UnsafeSourceError, OSError) as error:
            raise UnsafeArchiveEntryError("Archive produced an unsafe entry") from error
