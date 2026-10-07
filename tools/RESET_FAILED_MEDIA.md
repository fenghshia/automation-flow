# 独立重置失败的视频任务

`reset_failed_media.py` 只在手动运行时工作，不注册到主服务，不导入 Flask 应用、模型或调度器。通过现有 `EnvConfig` 读取配置，用 SQLAlchemy 直接连接 PostgreSQL；无需增加依赖。

它针对当前分组、当前配置和当前重置代次中仍存在的失败任务：

- `unclassified` 的失败特征提取任务：重新进入 `queued`，尝试次数清零，保留输入快照。服务重启后重新提取，再由原流程预测、分类和搬运。
- `liked_source` 的失败压缩任务：保留尝试次数、输出路径和血缘记录，有输出路径时进入 `processing` 恢复检查，否则进入 `waiting_stable`。没有可恢复成品时，原流程会重新从源视频处理。
- `--include-legacy`：允许接管源文件直接位于当前 `liked_source` 的未绑定旧失败压缩任务。仅在源大小和修改时间匹配、旧绑定字段为空、当前和旧输出目录没有该任务的成品或暂存文件、缓存没有该任务的工作文件时接管。

此处“重新处理”针对失败任务。不会删除特征摘要、模型、标签或血缘记录，不会重置成功任务，不会删除或覆盖视频。已有有效压缩成品可能被恢复复用，不强制重复编码；低码率源视频仍按原规则验证后复制。

## 使用方法

在仓库根目录执行，将 `example_group` 换成分组配置里的实际名称。

1. 预览，包含旧任务接管检查；只读事务，不更新数据库、不生成备份文件：

   ```bat
   mamba run -n autoflow python tools\reset_failed_media.py --group example_group --include-legacy
   ```

2. 查看 `targets` 和 `skipped`。`targets` 是可重置任务；`skipped` 给出未选中原因。预览与执行分别重新检查范围，不保证两次任务数量相同。

3. 停止 `automation-server` 及其所有 worker，再执行：

   ```bat
   mamba run -n autoflow python tools\reset_failed_media.py --group HMMD --include-legacy --apply --server-stopped
   ```

4. 看到 `Committed` 后，按原方式重新启动服务。脚本不会自动启动服务或处理视频。

只重置一种流水线时，增加 `--pipeline extract` 或 `--pipeline compression`。

## 保留与阻塞条件

执行前将选中任务的原状态、错误、尝试次数、绑定和输入快照保存为新的 `automation-server/private/task-resets/*.json`，该目录已被 Git 忽略。文件可能包含本地路径，只用于本机诊断；它的存在不代表事务已提交，也不是自动还原脚本。

数据库更新在单个事务内执行；发生异常会整体回滚。写入阶段使用压缩调度器同一把 advisory lock，并锁定相关表。`--server-stopped` 是操作者确认，不能自动证明服务已停止；仍需手动停掉服务。存在正在运行的筛选任务、未释放的 GPU 资源租约或锁冲突时拒绝执行，不擅自清理资源。

如果服务异常终止后仍残留运行任务或租约，先通过原服务的恢复机制处理，确认不再运行，再停止服务并重新预览。

以下情况会跳过任务：源文件不匹配或不可访问、链接或重解析点、旧配置或旧代次、已有同版本摘要、同视频已有提取排队任务、搬运冲突，以及旧压缩任务存在无法接管的产物。不会为让任务入选而清理这些数据。

重置不会修复媒体损坏、NVDEC 不支持的编码或超时。原问题仍存在时任务可能再次失败。尤其此前两个长视频已提取超时，需要另行评估处理耗时和时限。

## 离线验证

```bat
mamba run -n autoflow python -m unittest discover -s tools\tests -p test_reset_failed_media.py
```

测试只使用内存 SQLite 和临时目录，验证筛选、旧任务接管、数据保留及事务回滚，不连接实际数据库或调用 FFmpeg。PostgreSQL 的锁和实际任务恢复需在停止服务后人工验证。
