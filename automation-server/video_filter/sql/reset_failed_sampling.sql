-- PostgreSQL：重置失败的采样/特征提取任务，不执行训练或分类搬运。
-- 先停止 automation-server（含 worker），保存下方预览结果，再执行。
-- 默认只演练：末尾 ROLLBACK。确认范围后将它改为 COMMIT 才会保存。
-- 不支持的 NVDEC 格式、损坏媒体和超过时限的视频，重置后仍可能失败。
BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '30s';

CREATE TEMP TABLE sampling_reset_options ON COMMIT DROP AS
SELECT NULL::text AS group_name; -- NULL = 所有启用分组；可改为指定分组名称。

CREATE TEMP TABLE sampling_reset_targets ON COMMIT DROP AS
SELECT t.id, g.name AS group_name, t.attempts AS old_attempts, t.error_code AS old_error
FROM video_filter_task AS t
JOIN video_filter_dataset_group AS g
  ON g.id = t.dataset_group_id AND g.reset_epoch = t.reset_epoch AND g.enabled
JOIN video_filter_config_revision AS c
  ON c.id = t.config_revision_id AND c.dataset_group_id = g.id
 AND c.reset_epoch = g.reset_epoch AND c.active_slot = 'active' AND c.status = 'active'
CROSS JOIN sampling_reset_options AS o
WHERE t.kind = 'extract' AND t.status = 'failed'
  AND (o.group_name IS NULL OR g.name = o.group_name)
  AND EXISTS (
    SELECT 1 FROM video_filter_location AS l
    WHERE l.variant_id = t.variant_id AND l.dataset_group_id = g.id AND l.reset_epoch = g.reset_epoch
      AND l.status = 'present' AND l.role <> 'liked_source' AND l.current_path_key IS NOT NULL
      AND l.path = t.input_snapshot->>'path'
      AND l.size_bytes::text = t.input_snapshot->'source_snapshot'->>'size_bytes'
      AND l.modified_ns::text = t.input_snapshot->'source_snapshot'->>'modified_ns'
      AND l.file_identity::jsonb = (t.input_snapshot->'source_snapshot'->'file_identity')::jsonb
  )
  AND NOT EXISTS (
    SELECT 1 FROM video_filter_feature_bundle AS b
    WHERE b.variant_id = t.variant_id AND b.dataset_group_id = g.id AND b.reset_epoch = g.reset_epoch
      AND b.status = 'ready' AND b.feature_signature = t.input_snapshot->>'feature_signature'
  )
  AND NOT EXISTS (
    SELECT 1 FROM video_filter_task AS other
    WHERE other.id <> t.id AND other.kind = 'extract' AND other.variant_id = t.variant_id
      AND other.dataset_group_id = g.id AND other.reset_epoch = g.reset_epoch
      AND other.status IN ('queued', 'running')
  )
  AND NOT EXISTS (
    SELECT 1 FROM video_filter_transfer_operation AS op
    WHERE op.variant_id = t.variant_id AND op.dataset_group_id = g.id AND op.reset_epoch = g.reset_epoch
      AND op.status = 'conflict'
  )
  AND NOT EXISTS (
    SELECT 1 FROM media_resource_lease AS lease
    WHERE lease.status <> 'released'
      AND (lease.owner = t.id OR lease.owner LIKE 'filter:' || t.id || ':%')
  )
FOR UPDATE OF t;

-- 预览：保留原错误及次数，避免清除后失去诊断依据。
SELECT * FROM sampling_reset_targets ORDER BY group_name, id;
SELECT count(*) AS tasks_to_reset FROM sampling_reset_targets;

-- 新一轮手动重试预算从 0 开始；输入快照、摘要、标签、模型均保留。
UPDATE video_filter_task AS t
SET status = 'queued', attempts = 0, error_code = NULL,
    claim_token = NULL, claimed_at = NULL, heartbeat_at = NULL,
    execution_owner = NULL, finished_at = NULL
FROM sampling_reset_targets AS target
WHERE t.id = target.id AND t.kind = 'extract' AND t.status = 'failed'
RETURNING t.id, t.status, t.attempts;

-- 旧配置/旧重置代次、源位置变化、已有摘要、冲突或未释放租约的任务不会入选。
-- 本脚本不删除或释放租约；服务重启时由原恢复机制处理，再重新预览。
ROLLBACK;
