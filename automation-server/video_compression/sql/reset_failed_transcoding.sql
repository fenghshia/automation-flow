-- PostgreSQL：重置失败的转码任务，保留源文件、成品和血缘记录。
-- 先停止 automation-server（含 worker），保存下方预览结果，再执行。
-- 默认只演练：末尾 ROLLBACK。确认范围后将它改为 COMMIT 才会保存。
BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '30s';
-- 与转码调度器共用锁，避免它在本事务期间处理任务。
SELECT pg_advisory_xact_lock(6217575825034923344);

CREATE TEMP TABLE transcoding_reset_options ON COMMIT DROP AS
SELECT NULL::text AS group_name, FALSE AS include_legacy;
-- group_name：NULL = 所有启用分组；可改为指定分组名称。
-- include_legacy：仅独立转码模式需要；确认其当前输入/输出目录后才改为 TRUE。

CREATE TEMP TABLE transcoding_reset_targets ON COMMIT DROP AS
SELECT m.id, w.name AS group_name, m.attempts AS old_attempts,
       m.error_message AS old_error,
       CASE WHEN m.output_path IS NULL THEN 'waiting_stable' ELSE 'processing' END AS next_status
FROM video_compression_mission AS m
LEFT JOIN media_workflow_binding AS w ON w.id = m.workflow_id
CROSS JOIN transcoding_reset_options AS o
WHERE m.status = 'failed'
  AND (
    (w.enabled AND m.reset_epoch = w.reset_epoch AND m.directory_revision_id = w.directory_revision_id
      AND m.pinned_output_directory = w.destination_directory
      AND (o.group_name IS NULL OR w.name = o.group_name)
      AND EXISTS (
        SELECT 1 FROM video_filter_dataset_group AS g
        JOIN video_filter_config_revision AS c ON c.dataset_group_id = g.id AND c.reset_epoch = g.reset_epoch
        WHERE g.id = w.id AND g.enabled AND g.reset_epoch = w.reset_epoch
          AND c.id = w.directory_revision_id AND c.status = 'active' AND c.active_slot = 'active'
      ))
    OR (o.include_legacy AND o.group_name IS NULL AND m.workflow_id IS NULL)
  )
  AND NOT EXISTS (
    SELECT 1 FROM media_resource_lease AS lease
    WHERE lease.status <> 'released'
      AND (lease.owner LIKE 'compression:%' OR lease.mode = 'exclusive_compression')
  )
FOR UPDATE OF m;

-- 预览：next_status=processing 表示进入恢复检查，不表示直接重新编码。
SELECT * FROM transcoding_reset_targets ORDER BY group_name, id;
SELECT count(*) AS tasks_to_reset FROM transcoding_reset_targets;

UPDATE video_compression_mission AS m
SET status = target.next_status, stable_checks = 0, error_message = NULL,
    updated_at = timezone('UTC', CURRENT_TIMESTAMP)
FROM transcoding_reset_targets AS target
WHERE m.id = target.id AND m.status = 'failed'
RETURNING m.id, m.status, m.attempts;

-- 必须保留 attempts：压缩血缘 generation 由 mission.id + attempts 决定。
-- 必须保留 output_path 和绑定：恢复流程先校验暂存/已发布成品的归属；
-- 无可恢复成品才重新排队，冲突或文件仍损坏时会再次失败供人工核对。
-- 未认领（output_path 为空）的任务重新等待文件稳定。
-- SQL 不检查磁盘文件，也不清理租约或重置 processing/validating/cleanup_pending。
ROLLBACK;
