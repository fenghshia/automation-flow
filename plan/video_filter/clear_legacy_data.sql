-- PostgreSQL only. Stop Flask, workers and compression before executing.
-- Execute against the existing single-group schema BEFORE upgrading Alembic.
-- This removes only video_filter's 13 legacy business tables' rows.
-- It preserves videos, logs, model weights, shared lineage and compression data.
BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '60s';
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_attribute
             WHERE attrelid = 'video_filter_asset'::regclass
               AND attname = 'dataset_group_id' AND NOT attisdropped) THEN
    RAISE EXCEPTION 'This SQL targets legacy single-group data only; do not run after the grouped migration';
  END IF;
  IF EXISTS (SELECT 1 FROM video_filter_task WHERE status = 'running') THEN
    RAISE EXCEPTION 'Recover running video_filter tasks before clearing data';
  END IF;
  IF EXISTS (SELECT 1 FROM video_filter_transfer_operation
             WHERE status IN ('planned', 'destination_verified', 'published', 'conflict')) THEN
    RAISE EXCEPTION 'Resolve video_filter transfers before clearing their evidence';
  END IF;
END $$;
-- Intentionally no CASCADE: unexpected external references must stop cleanup.
TRUNCATE TABLE
  video_filter_prediction_outcome, video_filter_transfer_operation,
  video_filter_prediction, video_filter_task, video_filter_feedback_event,
  video_filter_feature_bundle, video_filter_location, video_filter_observation,
  video_filter_scan_run, video_filter_model_run, video_filter_variant,
  video_filter_asset, video_filter_config_revision;
COMMIT;
