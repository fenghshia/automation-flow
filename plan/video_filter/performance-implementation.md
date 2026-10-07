# video_filter 吞吐优化实施与部署记录

日期：2026-10-06。对应 [codingplan.md](codingplan.md) 的 P0—P8，诊断基线见 [research.md](research.md)。本轮未处理真实媒体、未清理数据、未执行生产数据库迁移，也未修改真实 `.env`。

## 实施结果

| 阶段 | 落地内容 |
| --- | --- |
| P0 | 固定旧日志窗口和既有隔离回归；保留已有工作区改动。旧日志数字仅作为改造前基线。 |
| P1 | 控制轮次输出队列、提取/其他任务数、等待年龄、发现/领取耗时；资源拒绝输出原因与显存预算；同一稳定状态限频，错误保留全堆栈。 |
| P2 | ResourceLease 持久化预算和测量信息；扣除未兑现预留后准入，提取/预测分别计数；保留独占互斥、未知 PID 的保守预算、OOM 提高预算与有效并发恢复。 |
| P3 | 清单/权重按文件身份缓存成功校验；并发校验串行去重，返回独立副本，300 秒完整重验；`clear_manifest_cache()` / `force=True` 可以强制失效。 |
| P4 | 提取、预测、分类分别维护有界队列，排除相同输入的 queued/running 任务；每批最多检查 100 个候选并跨轮分页；单轮按组轮询补位。 |
| P5 | 分类任务固定已提交的双预测 ID 和批次，重新验证完整模型集；兼容结果直接走 CPU 搬运配额；需新推理时提交预测后释放 GPU，任务继续跟踪到搬运结束。 |
| P6 | 推理期间关闭读取事务；入库前重验标签、模型字节和摘要校验值，保留全局锁序。扫描在长哈希前提交已核验身份，位置与喜欢反馈原子保存，持久记录中断扫描的到达证据以恢复移动对账。 |
| P7 | 单 worker 内用有界 RAM 复用 BEATs 的 16kHz 单声道窗口给 eGeMAPS；预算不足淘汰旧窗口并重新解码，无原始音频持久化，特征版本不变。 |
| P8 | 新增显存、缓存、补位、预测复用、事务与中断恢复测试；Jinja 工作台显示显存判定及队列；增加迁移和部署说明。最终测试结果见后文。 |

已有业务语义保持：按组独立 LR/MIL；每组每模型新增 100 个可训练资产再自动重训；完整摘要入库后预测；预测不作为标签；删除确认期沿用配置，默认 300 秒；liked_source 不执行特征采样；稳定期、配置代次、血缘与搬运所有权校验继续生效。

## 显存判定与配置

```text
未兑现预留 = Σ max(0, 活跃任务峰值预算 - 驱动可归属的受管进程显存)
可用空闲 = 驱动当前空闲 - 未兑现预留
允许启动：可用空闲 >= 新任务预计增量峰值 + 安全余量
```

驱动实际占用已体现在空闲值中，不再按任务数重复扣除；安全余量只应用一次。实际显存归属按受管 Job/进程组成员 PID 与启动身份核验。WDDM 返回 N/A、查询进程显存失败或 PID 身份不匹配时，该部分保留完整预留；驱动空闲查询失败则暂停准入。

| 配置 | 默认值 | 含义 |
| --- | --- | --- |
| `VIDEO_FILTER_EXTRACT_CONCURRENCY` | 6 | 全部组共享的提取数量上限 |
| `VIDEO_FILTER_GPU_EXTRACT_PEAK_MIB` | 1024 | 单个提取任务初始增量预算 |
| `VIDEO_FILTER_GPU_PREDICT_PEAK_MIB` | 256 | MIL 预测基础预算，另按窗口输入增加估算 |
| `VIDEO_FILTER_GPU_SAFETY_MIB` | 1024 | 全局剩余显存安全余量 |
| `VIDEO_FILTER_AUDIO_CACHE_MIB` | 32 | 单 worker 的音频窗口缓存上限；六路最多约 192 MiB |

这些值是启动估算，尚未经本机真实 GPU 性能校准。OOM 和可归属驱动高水位只提高兼容剖面的预算，不自动降低。剖面包含物理设备 UUID、特征签名、batch、工作类型、解码规格及已有的输入元数据；未完成摘要的首次提取没有可靠 codec/分辨率，使用 unknown 的保守共用剖面，不额外探测其他目录。MIL 预测还区分窗口数分桶。

提取全局最多配置数量，GPU 预测最多 2 个，分类搬运最多 2 个，训练最多 1 个。显存、独占等待或数据不足仍可能令实际并发低于 6；页面和日志给出原因。一次 OOM 暂降有效提取并发，至少 60 秒且累计成功达到当前上限两倍后逐步恢复。

## 部署步骤

1. 暂停筛选和压缩的任务发现，等待已有 worker 收尾，再停止主服务。确认没有仍活动的受管子进程；不要直接清空任务/租约表。
2. 在数据库备份后，于 `automation-server/` 执行迁移：

   ```cmd
   mamba run -n autoflow alembic upgrade head
   ```

   新迁移为 `d2e90b1746a3_add_gpu_memory_budgets.py`，前序为 `c4f18a2d9076`。只给 `media_resource_lease` 增加 `memory_budget_mib` 和 `memory_observation`，保留视频、摘要、模型、预测、反馈及租约记录。不需要旧数据清理或重新提取摘要。

3. 对照 `automation-server/.env.example` 配置上述可选参数；未配置时使用默认值。原分组 JSON、目录配置与相对路径根保持不变。
4. 重启主服务，打开 `/video_filter/dashboard/`。检查实际提取数、最老排队、最近显存判定与 `runtime.log` 的控制轮次、准入原因。模型权重应按只读文件部署；替换权重必须同步更新清单摘要。

回退前先让本轮 worker/租约正常收尾，再停止主服务。迁移 downgrade 拒绝存在 active/conflict 租约的情形；不要强行释放仍存活的进程。数据和预测历史无需删除。

## 只读诊断 SQL（PostgreSQL）

各组各类任务的队列与等待年龄：

```sql
SELECT g.name AS group_name, t.kind, t.status, COUNT(*) AS tasks,
       MAX(EXTRACT(EPOCH FROM (CURRENT_TIMESTAMP AT TIME ZONE 'UTC' - t.created_at))) AS oldest_age_seconds
FROM video_filter_task t
JOIN video_filter_dataset_group g
  ON g.id = t.dataset_group_id AND g.reset_epoch = t.reset_epoch
WHERE t.status IN ('queued', 'running')
GROUP BY g.name, t.kind, t.status
ORDER BY g.name, t.kind, t.status;
```

持久化租约预算和最近测量；这里的观察值是上次准入采样，不是实时驱动显存：

```sql
SELECT device, mode, status,
       memory_observation->>'workload_type' AS workload,
       memory_budget_mib,
       memory_observation->>'observed_mib' AS observed_mib,
       memory_observation->>'attribution' AS attribution,
       memory_observation->>'sampled_at' AS sampled_at,
       GREATEST(0, COALESCE(memory_budget_mib, 1024)
         - COALESCE((memory_observation->>'observed_mib')::integer, 0)) AS pending_reserved_mib
FROM media_resource_lease
WHERE status IN ('waiting', 'active', 'conflict')
ORDER BY device, created_at;
```

## 验证与限制

编码前基线：67 项隔离测试通过。最终实现验证结果：

- 受影响包、配置和新增迁移的 `compileall -q` 通过。
- video_filter 的全部 17 个 `test_*` 模块（含新增 `test_performance`）及公共 media_lineage 的 `test_events`、`test_operations`：**194 项通过，161.058 秒**。
- 仓库既有隔离压缩入口 `mamba run -n autoflow python -m video_filter.tests.compression_regression`：**38 项通过，0.321 秒**。
- `git diff --check` 通过；三份文档 UTF-8、代码围栏与本地引用检查通过；真实 `.env`、private 和运行日志仍被 Git 忽略。

完整隔离回归在 `automation-server/` 执行：

```cmd
mamba run -n autoflow python -m unittest video_filter.tests.test_performance video_filter.tests.test_manifest video_filter.tests.test_runtime video_filter.tests.test_tasks video_filter.tests.test_group_runtime video_filter.tests.test_groups video_filter.tests.test_migration video_filter.tests.test_dual_classifiers video_filter.tests.test_gpu_inference video_filter.tests.test_dashboard video_filter.tests.test_workflow video_filter.tests.test_feature_store video_filter.tests.test_feature_contract video_filter.tests.test_config video_filter.tests.test_configuration video_filter.tests.test_logging video_filter.tests.test_control media_lineage.tests.test_events media_lineage.tests.test_operations
```

异常分支测试主动制造数据库不可用、配置错误、文件消失、OOM 等情况，因此测试过程中可出现预期错误堆栈；以上结果均以最终进程退出码 0 和 unittest 的 OK 为准。

针对新增行为的测试覆盖：冷任务预留、已兑现预算、未知 PID/WDDM、查询失败、OOM/驱动峰值、旧租约和独占公平；单组及多组一轮六路补位；失败前部超过 20 项和队列上限；双预测复用与准确率分母；推理/复制期间无持锁事务及推理中反馈/参数变化；中断扫描的正反馈和移动恢复；权重缓存并发、周期重验、替换及验证竞态；音频等价、淘汰、短尾和无音轨；增量迁移保留原记录、活跃租约禁止回退。

未执行真实 GPU/媒体吞吐对比、生产 PostgreSQL 并发/锁测试及生产迁移。SQLite 测试证明状态转换和锁序调用，不能证明 PostgreSQL 并发性能。旧日志中的平均并发 1.392、峰值 3、159.148 秒观察空档不是新版测量结果。

本轮将文件复制、前置源哈希、暂存/目标哈希移到资产锁外，发布/清源前重新检查 gate；共享血缘服务在发布和清源时仍会进行最终全文件安全核验，该部分可能延长末段事务。保留它以维持恢复证据与源清理安全，后续若日志证明它成为瓶颈，应单独设计可复验的证据协议。
