import json
import os
import shutil
from collections import deque
from dataclasses import dataclass
from pathlib import Path

from .archive import ArchiveBudget, ArchiveExtractor
from .compressor import ImageProcessor
from .manifest import ContentManifest, content_manifest, iter_regular_files
from .naming import NameCandidate, allocate_names
from .policy import is_archive_name


class ImagePipelineError(RuntimeError):
    pass


@dataclass(frozen=True)
class PreparedBatch:
    result_path: Path
    destination: Path
    is_directory: bool
    manifest: ContentManifest


@dataclass(frozen=True)
class PublishPlan:
    staging_path: Path
    destination: Path
    is_directory: bool
    manifest: ContentManifest


@dataclass(frozen=True)
class LeafFile:
    key: str
    provenance: str
    path: Path


class ImageCompressionService:
    QUARANTINE_PREFIX = ".autoflow-image-"

    def __init__(
        self,
        source_directory,
        output_directory,
        seven_zip_bin_directory,
        pending_root=None,
        archive_extractor=None,
        image_processor=None,
    ):
        self.source_directory = Path(source_directory).resolve()
        self.output_directory = Path(output_directory).resolve()
        self.pending_root = Path(
            pending_root or Path(__file__).with_name("pending")
        ).resolve()
        self.archive_extractor = archive_extractor or ArchiveExtractor(
            seven_zip_bin_directory
        )
        self.image_processor = image_processor or ImageProcessor()
        if hasattr(self.image_processor, "validate_dependency"):
            self.image_processor.validate_dependency()
        self._validate_roots()

    def _validate_roots(self):
        if not self.source_directory.is_dir():
            raise ImagePipelineError("Image source directory does not exist")
        source = self.source_directory
        output = self.output_directory
        pending = self.pending_root
        roots = (source, output, pending)
        if any(
            left == right or left in right.parents or right in left.parents
            for index, left in enumerate(roots)
            for right in roots[index + 1 :]
        ):
            raise ImagePipelineError(
                "Image source, output, and pending directories must be separate and non-nested"
            )
        self.output_directory.mkdir(parents=True, exist_ok=True)
        self.pending_root.mkdir(parents=True, exist_ok=True)

    def mission_directory(self, mission_id):
        return self.pending_root / str(int(mission_id))

    def quarantine_path(self, mission_id):
        return self.source_directory / f"{self.QUARANTINE_PREFIX}{int(mission_id)}.pending"

    def publish_staging_path(self, mission_id):
        return self.output_directory / f"{self.QUARANTINE_PREFIX}{int(mission_id)}.part"

    @staticmethod
    def _direct_child(path, parent, description):
        path = Path(path).absolute()
        parent = Path(parent).resolve()
        if path.parent.resolve() != parent:
            raise ImagePipelineError(
                f"{description} is outside its configured directory"
            )
        return path

    @staticmethod
    def _remove_exact(path, allowed_parent):
        path = Path(path)
        allowed_parent = Path(allowed_parent).resolve()
        if path.absolute().parent.resolve() != allowed_parent:
            raise ImagePipelineError("Refusing to remove a path outside its owned parent")
        if path.is_symlink():
            path.unlink(missing_ok=True)
        elif path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink(missing_ok=True)

    @staticmethod
    def _copy_path(source, destination):
        source = Path(source)
        destination = Path(destination)
        if source.is_dir():
            shutil.copytree(
                source, destination, copy_function=shutil.copy2, symlinks=True
            )
        else:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)

    @staticmethod
    def _write_ingest_marker(mission_directory, manifest):
        marker = mission_directory / "ingest.json"
        temporary = mission_directory / "ingest.json.part"
        temporary.write_text(
            json.dumps(
                {
                    "digest": manifest.digest,
                    "file_count": manifest.file_count,
                    "size_bytes": manifest.size_bytes,
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        os.replace(temporary, marker)

    @staticmethod
    def _read_ingest_marker(mission_directory):
        marker = mission_directory / "ingest.json"
        try:
            data = json.loads(marker.read_text(encoding="utf-8"))
            return ContentManifest(
                data["digest"], int(data["file_count"]), int(data["size_bytes"])
            )
        except (FileNotFoundError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            # A missing or invalid marker is an expected recovery signal; callers
            # validate staged content before choosing a safe recovery path.
            return None

    def ensure_ingested(self, source_path, mission_id):
        source_path = Path(source_path)
        if source_path.parent.resolve() != self.source_directory:
            raise ImagePipelineError("Mission source is outside the configured source directory")

        mission_directory = self.mission_directory(mission_id)
        mission_directory.mkdir(parents=True, exist_ok=True)
        staged_source = mission_directory / "source"
        partial_source = mission_directory / "source.part"
        quarantine = self.quarantine_path(mission_id)

        if staged_source.exists():
            staged_manifest = content_manifest(staged_source)
            marker = self._read_ingest_marker(mission_directory)
            if marker is not None and marker != staged_manifest:
                raise ImagePipelineError("Pending source does not match its ingest marker")
            if quarantine.exists():
                if marker is None and content_manifest(quarantine) != staged_manifest:
                    raise ImagePipelineError("Quarantined source does not match pending source")
                self._remove_exact(quarantine, self.source_directory)
            if marker is None:
                self._write_ingest_marker(mission_directory, staged_manifest)
            return staged_source

        if partial_source.exists():
            self._remove_exact(partial_source, mission_directory)
        if not quarantine.exists():
            if not source_path.exists():
                raise ImagePipelineError("Source disappeared before it could be moved")
            source_path.rename(quarantine)

        quarantine_manifest = content_manifest(quarantine)
        self._copy_path(quarantine, partial_source)
        quarantine_after_copy = content_manifest(quarantine)
        partial_manifest = content_manifest(partial_source)
        if (
            quarantine_manifest != quarantine_after_copy
            or quarantine_manifest != partial_manifest
        ):
            raise ImagePipelineError("Pending copy does not match the quarantined source")
        partial_source.rename(staged_source)
        self._write_ingest_marker(mission_directory, partial_manifest)
        self._remove_exact(quarantine, self.source_directory)
        return staged_source

    def _reset_processing_paths(self, mission_id):
        mission_directory = self.mission_directory(mission_id)
        for name in ("unpack", "payload", "result"):
            path = mission_directory / name
            if path.exists():
                self._remove_exact(path, mission_directory)

    @staticmethod
    def _leaf_sort_key(leaf):
        return (leaf.provenance.casefold(), leaf.provenance)

    def _collect_leaves(self, staged_source, source_kind, source_name, mission_id):
        leaves = []
        archive_queue = deque()
        sequence = 0

        def add_leaf(path, provenance):
            nonlocal sequence
            sequence += 1
            leaves.append(LeafFile(str(sequence), provenance, Path(path)))

        if source_kind == "directory":
            for relative, path in iter_regular_files(staged_source):
                provenance = f"{source_name}/{relative.as_posix()}"
                if is_archive_name(path.name):
                    archive_queue.append((path, provenance, 1))
                else:
                    add_leaf(path, provenance)
        elif source_kind == "archive":
            archive_queue.append((Path(staged_source), source_name, 1))
        else:
            payload = self.mission_directory(mission_id) / "payload" / source_name
            self._copy_path(staged_source, payload)
            if content_manifest(staged_source) != content_manifest(payload):
                raise ImagePipelineError("Payload copy does not match the pending source")
            add_leaf(payload, source_name)

        unpack_root = self.mission_directory(mission_id) / "unpack"
        unpack_root.mkdir(parents=True, exist_ok=True)
        budget = ArchiveBudget()
        archive_number = 0
        while archive_queue:
            archive_path, provenance, depth = archive_queue.popleft()
            archive_number += 1
            extraction_directory = unpack_root / f"{archive_number:08d}"
            for relative, path in self.archive_extractor.extract(
                archive_path, extraction_directory, depth, budget
            ):
                member_provenance = f"{provenance}!/{relative.as_posix()}"
                if is_archive_name(path.name):
                    archive_queue.append((path, member_provenance, depth + 1))
                else:
                    add_leaf(path, member_provenance)
        return sorted(leaves, key=self._leaf_sort_key)

    def prepare_batch(
        self, mission_id, source_kind, source_name, destination_key
    ):
        mission_directory = self.mission_directory(mission_id)
        staged_source = mission_directory / "source"
        if not staged_source.exists():
            raise ImagePipelineError("Pending source is missing")
        self._reset_processing_paths(mission_id)
        leaves = self._collect_leaves(
            staged_source, source_kind, source_name, mission_id
        )
        if not leaves:
            raise ImagePipelineError("The source batch contains no leaf files")

        names = allocate_names(
            [NameCandidate(leaf.key, leaf.provenance, leaf.path.name) for leaf in leaves]
        )
        result_path = mission_directory / "result"
        result_path.mkdir()
        for leaf in leaves:
            self.image_processor.process(leaf.path, result_path / names[leaf.key])

        is_directory = source_kind in {"directory", "archive"}
        if is_directory:
            manifest = content_manifest(result_path)
        else:
            result_files = list(result_path.iterdir())
            if len(result_files) != 1 or not result_files[0].is_file():
                raise ImagePipelineError("A file mission produced an invalid result")
            manifest = content_manifest(result_files[0])
        if manifest.file_count != len(leaves):
            raise ImagePipelineError("Result file count does not match the source batch")
        destination = self.output_directory / destination_key
        return PreparedBatch(result_path, destination, is_directory, manifest)

    def stage_for_publish(self, mission_id, prepared):
        staging = self.publish_staging_path(mission_id)
        destination = self._direct_child(
            prepared.destination, self.output_directory, "Output path"
        )
        if staging.exists():
            self._remove_exact(staging, self.output_directory)
        if destination.exists():
            raise FileExistsError(
                f"Output already exists: {destination.name}"
            )

        if prepared.is_directory:
            self._copy_path(prepared.result_path, staging)
        else:
            result_files = list(prepared.result_path.iterdir())
            if len(result_files) != 1 or not result_files[0].is_file():
                raise ImagePipelineError("A file mission produced an invalid result")
            self._copy_path(result_files[0], staging)
        staged_manifest = content_manifest(staging)
        if staged_manifest != prepared.manifest:
            raise ImagePipelineError("Published staging copy does not match the result")
        return PublishPlan(
            staging, destination, prepared.is_directory, prepared.manifest
        )

    @staticmethod
    def validate_path(path, expected_manifest):
        actual = content_manifest(path)
        if actual != expected_manifest:
            raise ImagePipelineError("Output manifest does not match the expected result")
        return actual

    def publish(self, plan):
        destination = self._direct_child(
            plan.destination, self.output_directory, "Output path"
        )
        staging_path = self._direct_child(
            plan.staging_path, self.output_directory, "Publish staging path"
        )
        if destination.exists():
            self.validate_path(destination, plan.manifest)
            return destination
        if not staging_path.exists():
            raise ImagePipelineError("Validated publish staging path is missing")
        self.validate_path(staging_path, plan.manifest)
        staging_path.rename(destination)
        self.validate_path(destination, plan.manifest)
        return destination

    def recover_publish_plan(
        self, mission_id, destination, is_directory, expected_manifest
    ):
        return PublishPlan(
            self.publish_staging_path(mission_id),
            self._direct_child(destination, self.output_directory, "Output path"),
            is_directory,
            expected_manifest,
        )

    def cleanup_completed(self, mission_id):
        mission_directory = self.mission_directory(mission_id)
        if mission_directory.exists():
            self._remove_exact(mission_directory, self.pending_root)
        staging = self.publish_staging_path(mission_id)
        if staging.exists():
            self._remove_exact(staging, self.output_directory)

    def discard_publish_staging(self, mission_id):
        staging = self.publish_staging_path(mission_id)
        if staging.exists():
            self._remove_exact(staging, self.output_directory)
