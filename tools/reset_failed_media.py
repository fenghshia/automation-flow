"""Standalone, opt-in reset of failed media tasks; never imports the Flask app."""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
from uuid import uuid4

from sqlalchemy import create_engine, text

SERVER = Path(__file__).resolve().parents[1] / "automation-server"
sys.path.insert(0, str(SERVER))

from env import EnvConfig
from media_lineage.files import reject_media_links, snapshot

COMPRESSION_LOCK = 0x564944454F434D50


class ResetError(RuntimeError):
    pass


def rows(connection, sql, **parameters):
    return [dict(row) for row in connection.execute(text(sql), parameters).mappings()]


def object_value(value):
    return json.loads(value) if isinstance(value, str) else value


def normalized(path):
    return os.path.normcase(str(reject_media_links(path).resolve()))


def current_group(connection, settings):
    groups = rows(connection, """
        SELECT g.id, g.reset_epoch, c.id AS config_id, c.snapshot
        FROM video_filter_dataset_group g
        JOIN video_filter_config_revision c ON c.dataset_group_id = g.id
          AND c.reset_epoch = g.reset_epoch AND c.status = 'active'
          AND c.active_slot = 'active'
        WHERE g.name = :name AND g.enabled = true
    """, name=settings["name"])
    if len(groups) != 1:
        raise ResetError("group_requires_one_active_configuration")
    group = groups[0]
    recorded = object_value(group["snapshot"])
    expected = {role: normalized(path) for role, path in settings["directories"].items()}
    if recorded.get("name") != settings["name"] or recorded.get("directories") != expected:
        raise ResetError("current_configuration_not_reconciled")
    return group


def extraction_targets(connection, settings, group, skipped):
    parameters = {"g": group["id"], "e": group["reset_epoch"], "c": group["config_id"]}
    tasks = rows(connection, """
        SELECT t.*, l.path AS location_path, l.size_bytes AS location_size,
          l.modified_ns AS location_modified, l.file_identity AS location_identity,
          a.label_revision AS current_label_revision
        FROM video_filter_task t
        JOIN video_filter_location l ON l.variant_id = t.variant_id
          AND l.dataset_group_id = t.dataset_group_id AND l.reset_epoch = t.reset_epoch
          AND l.status = 'present' AND l.role = 'unclassified'
          AND l.current_path_key IS NOT NULL
        JOIN video_filter_asset a ON a.id = t.asset_id
          AND a.dataset_group_id = t.dataset_group_id AND a.reset_epoch = t.reset_epoch
        WHERE t.dataset_group_id = :g AND t.reset_epoch = :e
          AND t.config_revision_id = :c AND t.kind = 'extract' AND t.status = 'failed'
    """, **parameters)
    targets, seen = [], set()
    for task in tasks:
        if task["id"] in seen:
            continue
        seen.add(task["id"])
        inputs = object_value(task["input_snapshot"])
        try:
            path = Path(task["location_path"])
            if normalized(path.parent) != normalized(settings["directories"]["unclassified"]):
                raise ResetError("source_outside_unclassified")
            disk = snapshot(path)
            recorded = {"size_bytes": task["location_size"], "modified_ns": task["location_modified"],
                        "file_identity": object_value(task["location_identity"])}
            if inputs.get("path") != str(path) or inputs.get("source_snapshot") != disk or disk != recorded:
                raise ResetError("source_snapshot_changed")
            if inputs.get("label_revision") != task["current_label_revision"]:
                raise ResetError("label_revision_changed")
            checks = (
                ("ready_summary_exists", """SELECT id FROM video_filter_feature_bundle
                    WHERE dataset_group_id=:g AND reset_epoch=:e AND variant_id=:v
                      AND status='ready' AND feature_signature=:s"""),
                ("another_extraction_pending", """SELECT id FROM video_filter_task
                    WHERE dataset_group_id=:g AND reset_epoch=:e AND variant_id=:v
                      AND kind='extract' AND status IN ('queued','running')"""),
                ("transfer_conflict", """SELECT id FROM video_filter_transfer_operation
                    WHERE dataset_group_id=:g AND reset_epoch=:e AND variant_id=:v
                      AND status='conflict'"""),
            )
            for reason, sql in checks:
                if rows(connection, sql, **parameters, v=task["variant_id"], s=inputs.get("feature_signature")):
                    raise ResetError(reason)
        except (OSError, ValueError, ResetError) as error:
            skipped.append({"pipeline": "extract", "id": task["id"],
                            "reason": str(error) if isinstance(error, ResetError) else "source_unavailable"})
            continue
        targets.append({"pipeline": "extract", "id": task["id"], "old": task,
                        "next_status": "queued", "disk": disk})
    return targets


def compression_targets(connection, settings, group, include_legacy, skipped):
    if not settings.get("compression_enabled"):
        raise ResetError("group_compression_disabled")
    bindings = rows(connection, "SELECT * FROM media_workflow_binding WHERE id=:g", g=group["id"])
    if len(bindings) != 1:
        raise ResetError("compression_binding_missing")
    binding = bindings[0]
    source_root, output_root = (settings["directories"][role] for role in ("liked_source", "liked"))
    if (not binding["enabled"] or binding["reset_epoch"] != group["reset_epoch"] or
            binding["directory_revision_id"] != group["config_id"] or
            normalized(binding["source_directory"]) != normalized(source_root) or
            normalized(binding["destination_directory"]) != normalized(output_root)):
        raise ResetError("compression_binding_changed")
    tasks = rows(connection, """SELECT * FROM video_compression_mission
        WHERE status='failed' AND (workflow_id=:g OR workflow_id IS NULL)""", g=group["id"])
    targets = []
    for task in tasks:
        try:
            source = Path(task["source_path"])
            if normalized(source.parent) != normalized(source_root):
                continue
            if source.suffix.lower() != ".mp4" or source.name != task["file_name"]:
                raise ResetError("invalid_source_name")
            disk = snapshot(source)
            if (disk["size_bytes"], disk["modified_ns"]) != (task["size_bytes"], task["modified_ns"]):
                raise ResetError("source_snapshot_changed")
            destination = output_root / source.name
            legacy = task["workflow_id"] is None
            if legacy:
                if not include_legacy:
                    raise ResetError("legacy_requires_include_legacy")
                if any(task[key] is not None for key in ("reset_epoch", "directory_revision_id", "pinned_output_directory")):
                    raise ResetError("legacy_has_partial_binding")
                # Rebinding cannot transfer ownership of old outputs or staging files.
                artifacts = [destination, destination.with_name(f".{destination.name}.mission-{task['id']}.part"),
                             SERVER / "video_compression" / "cache" / f".mission-{task['id']}.transcoding.mp4"]
                if task["output_path"]:
                    old_output = Path(task["output_path"])
                    artifacts.extend((old_output, old_output.with_name(f".{old_output.name}.mission-{task['id']}.part")))
                if any(reject_media_links(p).exists() for p in artifacts):
                    raise ResetError("legacy_artifact_requires_manual_review")
                next_status = "waiting_stable"
            else:
                if (task["reset_epoch"], task["directory_revision_id"], task["pinned_output_directory"]) != (
                        binding["reset_epoch"], binding["directory_revision_id"], binding["destination_directory"]):
                    raise ResetError("mission_binding_changed")
                if task["output_path"] and normalized(task["output_path"]) != normalized(destination):
                    raise ResetError("mission_output_changed")
                next_status = "processing" if task["output_path"] else "waiting_stable"
        except (OSError, ValueError, ResetError) as error:
            skipped.append({"pipeline": "compression", "id": task["id"],
                            "reason": str(error) if isinstance(error, ResetError) else "source_unavailable"})
            continue
        targets.append({"pipeline": "compression", "id": task["id"], "old": task,
                        "next_status": next_status, "legacy": legacy, "binding": binding, "disk": disk})
    return targets


def build_plan(connection, settings, pipeline="both", include_legacy=False):
    group = current_group(connection, settings)
    targets, skipped = [], []
    if pipeline in ("extract", "both"):
        targets.extend(extraction_targets(connection, settings, group, skipped))
    if pipeline in ("compression", "both"):
        targets.extend(compression_targets(connection, settings, group, include_legacy, skipped))
    return targets, skipped


def apply_targets(connection, targets):
    for target in targets:
        old = target["old"]
        path = old["location_path"] if target["pipeline"] == "extract" else old["source_path"]
        if snapshot(path) != target["disk"]:
            raise ResetError("source_changed_after_preview")
        if target["pipeline"] == "extract":
            sql = """UPDATE video_filter_task SET status='queued', attempts=0, error_code=NULL,
                claim_token=NULL, claimed_at=NULL, heartbeat_at=NULL, execution_owner=NULL, finished_at=NULL
                WHERE id=:id AND status='failed' AND dataset_group_id=:g AND reset_epoch=:e
                  AND config_revision_id=:c"""
            parameters = {"id": target["id"], "g": old["dataset_group_id"],
                          "e": old["reset_epoch"], "c": old["config_revision_id"]}
        else:
            binding = target["binding"]
            sql = """UPDATE video_compression_mission SET status=:status, stable_checks=0,
                error_message=NULL, workflow_id=:g, reset_epoch=:e, directory_revision_id=:c,
                pinned_output_directory=:output, output_path=:path, updated_at=:now
                WHERE id=:id AND status='failed'"""
            parameters = {"id": target["id"], "status": target["next_status"], "g": binding["id"],
                          "e": binding["reset_epoch"], "c": binding["directory_revision_id"],
                          "output": binding["destination_directory"],
                          "path": None if target["legacy"] else old["output_path"],
                          "now": datetime.now(timezone.utc).replace(tzinfo=None)}
        if connection.execute(text(sql), parameters).rowcount != 1:
            raise ResetError("task_changed_during_reset")


def public_plan(targets, skipped):
    return {"targets": [{"pipeline": t["pipeline"], "id": t["id"], "next_status": t["next_status"],
                         "previous_attempts": t["old"]["attempts"],
                         "previous_error_code": t["old"].get("error_code"),
                         "adopt_legacy": t.get("legacy", False)} for t in targets], "skipped": skipped}


def write_backup(targets):
    directory = SERVER / "private" / "task-resets"
    reject_media_links(directory).mkdir(parents=True, exist_ok=True)
    name = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex + ".json"
    with (directory / name).open("x", encoding="utf-8") as stream:
        json.dump({"purpose": "pre-reset snapshot; existence does not prove commit",
                   "tasks": targets}, stream, ensure_ascii=False, indent=2, default=str)
    return name


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--group", required=True, help="Exact configured group name")
    parser.add_argument("--pipeline", choices=("both", "extract", "compression"), default="both")
    parser.add_argument("--include-legacy", action="store_true", help="Adopt unbound failed compression missions after artifact checks")
    parser.add_argument("--apply", action="store_true", help="Commit resets; default is a read-only preview")
    parser.add_argument("--server-stopped", action="store_true", help="Confirm the server and its workers have been stopped")
    args = parser.parse_args(argv)
    if args.apply and not args.server_stopped:
        parser.error("--apply requires stopping the server/workers and adding --server-stopped")
    engine = None
    try:
        settings = EnvConfig.video_filter_settings(ignore_scope=True)
        if not settings.get("enabled") or not settings.get("grouped"):
            raise ResetError("enabled_grouped_configuration_required")
        group = next((g for g in settings["groups"] if g["name"] == args.group and g["enabled"]), None)
        if group is None:
            raise ResetError("enabled_group_not_found")
        engine = create_engine(EnvConfig.database_uri(), connect_args={"connect_timeout": 5}, hide_parameters=True)
        if engine.dialect.name != "postgresql":
            raise ResetError("postgresql_required")
        with engine.connect() as connection:
            with connection.begin():
                if not args.apply:
                    connection.execute(text("SET TRANSACTION READ ONLY"))
                connection.execute(text("SET LOCAL statement_timeout='30s'"))
                if args.apply:
                    if not connection.scalar(text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": COMPRESSION_LOCK}):
                        raise ResetError("compression_scheduler_busy")
                    connection.execute(text("""LOCK TABLE video_filter_task, video_filter_location,
                        video_filter_asset, video_filter_config_revision, video_filter_dataset_group,
                        video_filter_feature_bundle, video_filter_transfer_operation, video_compression_mission,
                        media_workflow_binding, media_resource_lease, media_lineage_event IN EXCLUSIVE MODE NOWAIT"""))
                    if rows(connection, "SELECT id FROM video_filter_task WHERE status='running' LIMIT 1"):
                        raise ResetError("running_workers_require_recovery_before_reset")
                    if rows(connection, "SELECT id FROM media_resource_lease WHERE status <> 'released' LIMIT 1"):
                        raise ResetError("unreleased_resource_leases_require_recovery_before_reset")
                targets, skipped = build_plan(connection, group, args.pipeline, args.include_legacy)
                print(json.dumps(public_plan(targets, skipped), ensure_ascii=True, indent=2))
                if args.apply and targets:
                    backup = write_backup(targets)
                    print("Pre-reset snapshot: automation-server/private/task-resets/" + backup)
                    apply_targets(connection, targets)
            print(("Committed" if args.apply else "Preview only; no changes") + f": {len(targets)} task(s).")
        return 0
    except Exception as error:
        # Connection errors can contain credentials; never print their raw text.
        code = str(error) if isinstance(error, ResetError) else type(error).__name__
        print("Reset aborted; database changes rolled back. reason=" + code, file=sys.stderr)
        return 1
    finally:
        if engine is not None:
            engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
