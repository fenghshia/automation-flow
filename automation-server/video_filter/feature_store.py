"""Numerical summaries and metadata committed together in the local database."""

import hashlib
import io
import zipfile
from dataclasses import dataclass
from uuid import UUID

import numpy as np

from .features.contract import (
    DIMENSIONS, MODALITIES, SCHEMA_VERSION, FeatureSignature, canonical_json, is_sha256,
)


MAX_PAYLOAD_BYTES = 512 * 1024 * 1024
PAYLOAD_FORMAT = "npz-v1"


def _uuid(value):
    if not isinstance(value, str) or str(UUID(value)) != value:
        raise ValueError("Canonical UUID required.")
    return value


def _summaries(vectors, valid):
    counts = valid.sum(axis=0)
    mean = np.divide(
        np.where(valid, vectors, 0).sum(axis=0, dtype=np.float64), counts,
        out=np.zeros(vectors.shape[1], dtype=np.float64), where=counts > 0,
    )
    variance = np.divide(
        np.where(valid, (vectors - mean) ** 2, 0).sum(axis=0, dtype=np.float64), counts,
        out=np.zeros(vectors.shape[1], dtype=np.float64), where=counts > 0,
    )
    return mean.astype(np.float32), np.sqrt(variance).astype(np.float32)


def _validate_arrays(arrays, duration, audio_status):
    if isinstance(duration, bool) or not isinstance(duration, (int, float)) or not np.isfinite(duration) or duration <= 0:
        raise ValueError("Invalid media duration.")
    if audio_status not in ("present", "no_audio"):
        raise ValueError("Audio absence must be distinguished from extraction failure.")
    windows = arrays.get("windows")
    if not isinstance(windows, np.ndarray) or windows.dtype != np.float64 or windows.ndim != 2 or windows.shape[1] != 2:
        raise ValueError("Window intervals must be an N x 2 float64 array.")
    count = len(windows)
    if not 1 <= count <= 100000 or not np.isfinite(windows).all():
        raise ValueError("Invalid window count or intervals.")
    if (windows[:, 0] < 0).any() or (windows[:, 1] <= windows[:, 0]).any() or (windows[:, 1] > duration).any():
        raise ValueError("Windows fall outside the media duration.")
    if windows[0, 0] != 0 or windows[-1, 1] != duration or (count > 1 and not np.array_equal(windows[1:, 0], windows[:-1, 1])):
        raise ValueError("Incomplete temporal coverage.")
    expected = {"windows"}
    for name in MODALITIES:
        expected.update((name, name + "_valid", name + "_mean", name + "_std"))
        vector, valid = arrays.get(name), arrays.get(name + "_valid")
        shape = (count, DIMENSIONS[name])
        if not isinstance(vector, np.ndarray) or vector.dtype != np.float32 or vector.shape != shape or not np.isfinite(vector).all():
            raise ValueError("Feature vectors must have the declared finite float32 shape.")
        if not isinstance(valid, np.ndarray) or valid.dtype != np.bool_ or valid.shape != shape:
            raise ValueError("Per-dimension validity masks are required.")
        if (vector[~valid] != 0).any():
            raise ValueError("Invalid features must be zero with an explicit mask.")
        if name in ("dino", "videomae") and not valid.all():
            raise ValueError("Visual extraction must complete for every window.")
        if audio_status == "no_audio" and name in ("beats", "egemaps") and valid.any():
            raise ValueError("No-audio summaries cannot contain valid audio features.")
        if audio_status == "present" and name == "beats" and not valid.all():
            raise ValueError("Audio embedding extraction must complete when audio is present.")
        for suffix, value in zip(("_mean", "_std"), _summaries(vector, valid)):
            summary = arrays.get(name + suffix)
            if not isinstance(summary, np.ndarray) or summary.dtype != np.float32 or summary.shape != (DIMENSIONS[name],) or not np.array_equal(summary, value):
                raise ValueError("Aggregate summary does not match window features.")
    if set(arrays) != expected:
        raise ValueError("Unexpected summary arrays.")
    return count


def _validity(arrays):
    return {
        name: {"valid_values": int(arrays[name + "_valid"].sum()),
               "total_values": int(arrays[name + "_valid"].size)}
        for name in MODALITIES
    }


def _snapshot(value):
    if not isinstance(value, dict) or set(value) != {"size_bytes", "modified_ns"}:
        raise ValueError("Source stability snapshot required.")
    if any(type(item) is not int or item < 0 for item in value.values()) or value["size_bytes"] == 0:
        raise ValueError("Invalid source stability snapshot.")


@dataclass(frozen=True)
class PreparedBundle:
    """In-memory worker output, with no persisted/ready guarantee."""

    arrays_blob: bytes
    manifest: dict
    manifest_sha256: str


@dataclass(frozen=True)
class StoredBundle:
    bundle_id: str
    manifest_sha256: str
    manifest: dict
    arrays: dict


class FeatureStore:
    def prepare(self, *, asset_id, variant_id, source_sha256, task_id, signature,
                source_snapshot, duration_seconds, windows, vectors, validity, audio_status,
                dataset_group_id=None, reset_epoch=None):
        """Serialize into memory; never write NPZ or manifest files beside videos."""
        for value in (asset_id, variant_id, task_id):
            _uuid(value)
        if not is_sha256(source_sha256) or not isinstance(signature, FeatureSignature):
            raise ValueError("Verified source and feature signature required.")
        _snapshot(source_snapshot)
        if set(vectors) != set(MODALITIES) or set(validity) != set(MODALITIES):
            raise ValueError("All four modalities are required.")
        arrays = {"windows": np.asarray(windows, dtype=np.float64)}
        for name in MODALITIES:
            vector, valid = np.asarray(vectors[name]), np.asarray(validity[name])
            if vector.dtype != np.float32 or valid.dtype != np.bool_ or vector.ndim != 2 or valid.shape != vector.shape:
                raise ValueError("Invalid feature/validity shapes.")
            arrays[name], arrays[name + "_valid"] = vector, valid
            arrays[name + "_mean"], arrays[name + "_std"] = _summaries(vector, valid)
        count = _validate_arrays(arrays, duration_seconds, audio_status)
        if sum(value.nbytes for value in arrays.values()) > MAX_PAYLOAD_BYTES:
            raise ValueError("Summary exceeds the size limit.")
        output = io.BytesIO()
        np.savez_compressed(output, **arrays)
        blob = output.getvalue()
        manifest = {
            "schema_version": SCHEMA_VERSION, "complete": True,
            "asset_id": asset_id, "variant_id": variant_id, "task_id": task_id,
            "source_sha256": source_sha256, "source_snapshot": source_snapshot,
            "duration_seconds": duration_seconds, "audio_status": audio_status,
            "feature_signature": signature.digest, "specification": signature.to_dict(),
            "window_count": count, "arrays_sha256": hashlib.sha256(blob).hexdigest(),
            "modality_validity": _validity(arrays),
        }
        if dataset_group_id is not None or reset_epoch is not None:
            manifest.update(dataset_group_id=_uuid(dataset_group_id), reset_epoch=_uuid(reset_epoch))
        # Isolate caller-owned dictionaries before producing a worker result.
        import json

        encoded = canonical_json(manifest).encode("utf-8")
        prepared = PreparedBundle(blob, json.loads(encoded), hashlib.sha256(encoded).hexdigest())
        self._decode(prepared)
        return prepared

    def _decode(self, prepared):
        manifest, blob = prepared.manifest, prepared.arrays_blob
        if not isinstance(manifest, dict) or set(manifest) - {"dataset_group_id", "reset_epoch"} != {
            "schema_version", "complete", "asset_id", "variant_id", "task_id",
            "source_sha256", "source_snapshot", "duration_seconds", "audio_status",
            "feature_signature", "specification", "window_count", "arrays_sha256", "modality_validity",
        } or type(manifest["schema_version"]) is not int or manifest["schema_version"] != SCHEMA_VERSION or manifest["complete"] is not True:
            raise ValueError("Incomplete or unsupported summary.")
        encoded = canonical_json(manifest).encode("utf-8")
        if len(encoded) > 1024 * 1024 or hashlib.sha256(encoded).hexdigest() != prepared.manifest_sha256:
            raise ValueError("Manifest checksum mismatch.")
        for key in ("asset_id", "variant_id", "task_id"):
            _uuid(manifest[key])
        if "dataset_group_id" in manifest or "reset_epoch" in manifest:
            _uuid(manifest.get("dataset_group_id"))
            _uuid(manifest.get("reset_epoch"))
        if not is_sha256(manifest["source_sha256"]):
            raise ValueError("Invalid source digest.")
        _snapshot(manifest["source_snapshot"])
        signature = FeatureSignature(**manifest["specification"])
        if signature.digest != manifest["feature_signature"]:
            raise ValueError("Feature signature mismatch.")
        if not isinstance(blob, bytes) or not blob or len(blob) > MAX_PAYLOAD_BYTES or hashlib.sha256(blob).hexdigest() != manifest["arrays_sha256"]:
            raise ValueError("Array checksum or size mismatch.")
        expected = {"windows.npy"}
        for name in MODALITIES:
            expected.update(name + suffix + ".npy" for suffix in ("", "_valid", "_mean", "_std"))
        try:
            with zipfile.ZipFile(io.BytesIO(blob)) as archive:
                entries = archive.infolist()
                if len(entries) != len(expected) or {entry.filename for entry in entries} != expected or sum(entry.file_size for entry in entries) > MAX_PAYLOAD_BYTES:
                    raise ValueError("Unexpected or oversized summary archive.")
            with np.load(io.BytesIO(blob), allow_pickle=False) as content:
                arrays = {name: content[name] for name in content.files}
        except (OSError, zipfile.BadZipFile, EOFError) as error:
            raise ValueError("Invalid summary archive.") from error
        count = _validate_arrays(arrays, manifest["duration_seconds"], manifest["audio_status"])
        if type(manifest["window_count"]) is not int or count != manifest["window_count"] or manifest["modality_validity"] != _validity(arrays):
            raise ValueError("Window count or modality validity mismatch.")
        return arrays

    def save(self, session, prepared):
        """Own the transaction: commit blob, metadata and ready state together.

        A failed commit rolls back all changes. The original video stays in place;
        the worker can retry using its PreparedBundle or extract again after restart.
        """
        from sqlalchemy import select
        from sqlalchemy.exc import IntegrityError
        from .models import FeatureBundle, Variant

        self._decode(prepared)
        manifest = prepared.manifest
        from .scope import current_scope
        scope = current_scope()
        if scope and (manifest.get("dataset_group_id"), manifest.get("reset_epoch")) != (scope["id"], scope["epoch"]):
            raise ValueError("cross_group_or_stale_epoch_summary")
        query = select(FeatureBundle).filter_by(
            variant_id=manifest["variant_id"], feature_signature=manifest["feature_signature"],
        )
        new_record = False
        try:
            variant = session.get(Variant, manifest["variant_id"])
            if variant is None or variant.asset_id != manifest["asset_id"] or variant.sha256 != manifest["source_sha256"] or variant.size_bytes != manifest["source_snapshot"]["size_bytes"]:
                raise ValueError("Summary identity does not match the registered source.")
            record = session.execute(query.with_for_update()).scalar_one_or_none()
            if record is None:
                new_record = True
                record = FeatureBundle(variant_id=variant.id, feature_signature=manifest["feature_signature"])
                session.add(record)
            elif record.status == "ready":
                if record.manifest_sha256 != prepared.manifest_sha256 or record.arrays_blob != prepared.arrays_blob:
                    raise ValueError("A different immutable summary is already registered.")
                record_id = record.id
                session.commit()
                return self.require_ready(session, record_id)
            record.arrays_blob = prepared.arrays_blob
            record.arrays_sha256 = manifest["arrays_sha256"]
            record.manifest = manifest
            record.manifest_sha256 = prepared.manifest_sha256
            record.payload_format = PAYLOAD_FORMAT
            record.windows = manifest["window_count"]
            record.modality_validity = manifest["modality_validity"]
            record.relative_path = None
            record.status = "ready"
            session.flush()
            record_id = record.id
            session.commit()
        except IntegrityError:
            session.rollback()
            if not new_record:
                raise
            # Another publisher may have won the unique variant/signature insert.
            record = session.execute(query).scalar_one_or_none()
            if record is None or record.status != "ready" or record.manifest_sha256 != prepared.manifest_sha256 or record.arrays_blob != prepared.arrays_blob:
                session.rollback()
                raise
            record_id = record.id
        except Exception:
            session.rollback()
            raise
        return self.require_ready(session, record_id)

    def require_ready(self, session, bundle_id):
        """Decode only committed DB payloads; an ORM flush cannot open the gate."""
        from sqlalchemy import select
        from .models import FeatureBundle, Variant
        from .scope import group_default, epoch_default, current_scope
        group_id, epoch = group_default(), epoch_default()
        with session.get_bind().connect() as connection:
            committed = connection.execute(select(
                FeatureBundle.id, FeatureBundle.status, FeatureBundle.arrays_blob,
                FeatureBundle.arrays_sha256, FeatureBundle.manifest,
                FeatureBundle.manifest_sha256, FeatureBundle.payload_format,
                FeatureBundle.variant_id, FeatureBundle.feature_signature,
                FeatureBundle.windows, FeatureBundle.modality_validity,
                Variant.asset_id, Variant.sha256, Variant.size_bytes,
            ).join(Variant, FeatureBundle.variant_id == Variant.id).where(FeatureBundle.id == bundle_id,
                FeatureBundle.dataset_group_id == group_id, Variant.dataset_group_id == group_id,
                FeatureBundle.reset_epoch == epoch, Variant.reset_epoch == epoch)).first()
        if committed is None or committed.status != "ready" or committed.payload_format != PAYLOAD_FORMAT:
            raise ValueError("A committed database summary is required.")
        blob = bytes(committed.arrays_blob) if committed.arrays_blob is not None else None
        prepared = PreparedBundle(blob, committed.manifest, committed.manifest_sha256)
        arrays = self._decode(prepared)
        manifest = prepared.manifest
        if current_scope() and (manifest.get("dataset_group_id"), manifest.get("reset_epoch")) != (group_id, epoch):
            raise ValueError("cross_group_or_stale_epoch_summary")
        if manifest["variant_id"] != committed.variant_id or manifest["source_sha256"] != committed.sha256 or manifest["asset_id"] != committed.asset_id or manifest["feature_signature"] != committed.feature_signature or manifest["source_snapshot"]["size_bytes"] != committed.size_bytes:
            raise ValueError("Ready summary identity mismatch.")
        if manifest["arrays_sha256"] != committed.arrays_sha256 or manifest["window_count"] != committed.windows or manifest["modality_validity"] != committed.modality_validity:
            raise ValueError("Ready summary metadata mismatch.")
        return StoredBundle(committed.id, committed.manifest_sha256, manifest, arrays)
