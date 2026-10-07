# video_filter 多组实现与启用说明

日期：2026-10-05。对应 [codingplan.md](codingplan.md) 的 P0–P8。本次提供代码、迁移和隔离验收；生产清理、生产迁移与真实媒体操作没有执行。

## 已落地的行为

| 阶段 | 实现 |
| --- | --- |
| P0 | 保留现有四路特征与 LR/MIL 契约，检查调用链，旧回归基线 105 项通过 |
| P1 | [clear_legacy_data.sql](clear_legacy_data.sql)，用户自行执行；没有清理程序 |
| P2 | EnvConfig 读取私有组 JSON；不可变 DatasetGroup UUID、RuntimeState epoch、组查询保护与组合外键；新迁移拒绝旧数据残留 |
| P3 | 五角色平铺追踪、删除消费与恢复确认、路径变更基线、非递归 Windows 通知及溢出保护；liked_source 不采样 |
| P4 | 中立 WorkflowBinding；压缩任务固定组、代次、目录版本、目标；公共血缘关联原版/重编码版与受控清源 |
| P5 | 默认全局六个视频槽、独立 subprocess、独立 session；短调度 tick 与独立对账；数据库资源请求/租约、GPU UUID、显存准入、独占训练/压缩、Windows Job Object |
| P6 | 每组 LR/MIL 独立训练与活动版本，模型类型各自超参 digest；完整有效参数与数值模型进数据库；双预测、实际反馈与共同样本准确率 |
| P7 | 显式分组 API、分页任务、Jinja 组导航、六个并行进度、资源等待和超参展示；日志带组 UUID、epoch、task；error.log 保留脱敏堆栈 |
| P8 | 隔离数据库/故障测试、真实 Windows 合成进程树测试、隔离浏览器检查、限定组内文件的诊断/采样/基准工具、启用说明 |

实现选择：ConfigRevision 保存目录版本，Task.input_snapshot 保存模型类型的有效训练配置，ModelRun 保存该配置 digest、完整参数、验证集合和版本；没有再建立一份可漂移的独立训练配置来源。旧 Prediction.group_id 的兼容属性仍可在 Python 中读取，数据库实际列为 prediction_batch_id。

## 配置与启动

所有相对路径以 `automation-server/` 为根。真实 `.env` 本次未改，示例已同步。复制 [groups.example.json](../../automation-server/video_filter/groups.example.json) 到被忽略的 `automation-server/private/video_filter_groups.json`，填写实际五个目录与稳定组名。

```dotenv
VIDEO_FILTER_ENABLED=true
VIDEO_FILTER_GROUPS_CONFIG=private/video_filter_groups.json
VIDEO_FILTER_STATE_DIR=video_filter/state
VIDEO_FILTER_MODEL_MANIFEST=video_filter/weights/manifest.json
VIDEO_FILTER_DEVICE=cuda:0
VIDEO_FILTER_EXTRACT_CONCURRENCY=6
VIDEO_FILTER_WORKER_CPU_THREADS=1
```

FFmpeg、模型清单及本地权重沿用原准备方式；本次没有安装或下载新依赖。旧单组目录、分类器和三个开关变量不再作为生产分组配置的回退。组内 classifier、training、transfer_enabled、compression_enabled 以 JSON 为准；血缘与删除反馈始终启用。

启用顺序：

1. 停止 Flask、worker 与压缩，先恢复或核对未完成操作。不要直接清掉冲突证据。
2. 备份需要保留的旧筛选结果后，审查并在旧单组数据库执行 clear_legacy_data.sql。SQL 会拒绝运行任务、未完成搬运和已升级的分组 schema；白名单 13 表、不用 CASCADE。
3. 从 `automation-server/` 在确认的数据库上执行 `mamba run -n autoflow alembic upgrade head`。新 head 为 `c4f18a2d9076`；升级只允许空的旧筛选业务表，公共血缘与压缩记录保留。
4. 填写私有组 JSON。初始保持 transfer_enabled=false；按需要设置 compression_enabled。
5. 人工启动 `mamba run -n autoflow python main.py`。首次控制为每组建立 UUID/epoch 与公共血缘水位；完整目录扫描后开始提取。调度 tick 为 2 秒，完整扫描间隔默认 60 秒。
6. 每组正负完整摘要达到 training.minimum_per_class 后自动独立训练。两套可用模型都记录预测；只有所选 classifier 可用且搬运开关打开才路由文件。

分组压缩模式只发现明确启用绑定的 liked_source → liked；不会从旧全局压缩目录猜测筛选组。原独立压缩入口仍保留，但启用多组调度后不会额外枚举未声明的全局媒体目录。

如果 SQL 因旧 running 任务拒绝，先确认相应进程确实已经停止。纯扫描、提取、训练、预测任务可按核对过的任务 ID 手动处理；分类任务与未完成搬运仍须先恢复文件操作：

```sql
SELECT id, kind, status FROM video_filter_task WHERE status = 'running';
-- 仅替换为已核对、已停止的任务 ID；不要对在途分类任务使用。
UPDATE video_filter_task
SET status = 'failed', error_code = 'operator_confirmed_stopped', finished_at = CURRENT_TIMESTAMP
WHERE id = '<confirmed_stale_task_id>' AND status = 'running'
  AND kind IN ('scan', 'extract', 'train', 'predict');
```

## 标签、路径与恢复

删除任一已登记位置，经过完整对账和缺失等待后记为 0；同组其他存活副本不能阻止该负反馈。删除事件消费后退休该位置，之后再次确认喜欢不会被旧缺失记录反复否定。liked/liked_source 的新用户迁入记为 1，预测目录不产生训练标签。

摘要不随文件删除而删除。分类搬运必须在完整摘要和预测已提交后执行。用户在提取完整核验后、入库前删除时，可保存原版本摘要；提取途中删除/替换导致失败的残缺数据不能标 ready。用户在文件尚未稳定登记或摘要未完成前自行删除，程序无法补出不存在的特征；资产与元数据记录也受是否已经登记影响。

保持 name 不变即可更换路径；新基线保留已有标签，不访问旧根。禁用/移除组不制造删除。重新加回同名组可继续原数据。改名会形成新的数据集，不自动转移旧数据。

组外未知消失按本组缺失处理；跨组不会合并资产、共享标签或继承特征。尚未完成的搬运/压缩血缘超时会显示追踪冲突，保留证据，需核对后通过明确反馈处理。通知溢出与磁盘离线暂停删除推断，避免把漏事件解释为删除。

状态文件改用 `state/groups/<dataset_group_id>/<reset_epoch>/`，旧根 journal 不重放。新组忽略水位之前以及未携带正确组/epoch 的公共事件；原公共事件没有被删除。

## 查询与日志

工作台地址：`/video_filter/dashboard/`，通过组链接查看本组。页面自动刷新保持当前组，展示全局六条任务；暂停、手动刷新、失败保留上次结果继续可用。

API：`GET /video_filter/groups`；`/video_filter/groups/<name>/status`、assets、tasks、tasks/<id>、metrics。scan/extract/train/predict/classify/feedback/retry 使用同样明确的组前缀。旧无组写接口返回 group_required，不默认第一组。

以下 SQL 只读查询摘要覆盖与用户标签，不加载特征 BLOB：

```sql
SELECT g.name, a.id AS asset_id, a.label, a.label_revision,
       v.id AS variant_id, l.role, l.status,
       b.id AS bundle_id, b.status AS summary_status, b.windows
FROM video_filter_dataset_group g
JOIN video_filter_asset a ON a.dataset_group_id = g.id
JOIN video_filter_variant v ON v.asset_id = a.id AND v.dataset_group_id = g.id
LEFT JOIN video_filter_location l ON l.variant_id = v.id AND l.dataset_group_id = g.id
LEFT JOIN video_filter_feature_bundle b ON b.variant_id = v.id AND b.dataset_group_id = g.id
WHERE g.name = 'example_group'
ORDER BY v.created_at DESC;
```

摘要可用以 feature_bundle.status=ready 为准。日志中的“特征摘要已提交并验证可读”带 task_id、variant_id、bundle_id 及组身份。数值特征仍在 feature_bundle.arrays_blob，模型参数在 model_run.model_blob；临时 NPZ 仅用于进程传输，正常结束会清除。

runtime.log 记录阶段、训练、反馈、预测和任务终态；error.log 单独保留完整堆栈与远程 worker 的异常链，隐藏 SQL 参数与已知私有路径。worker 的 stdout 由主进程转发，避免多进程共同轮转日志。

## 并发与实际限制

6 是配置上限。资源仲裁统一到驱动 GPU UUID；NVENC 按物理适配器 0 协调，CUDA 处理 CUDA_VISIBLE_DEVICES。每次准入检查驱动空闲显存，采用每个活动共享槽 2048 MiB 的保守余量；显存不足时保留 queued，页面分别显示配置上限与实际准入、独占等待。

同一视频在独立 worker 内顺序完成四路并释放阶段模型。MIL 训练与压缩独占，已有共享提取/预测先结束。MIL 预测与提取共享准入。OOM 降低运行上限并有限重试，不切换模型、采样规格或 CPU。该余量是初始策略，不是 3060 Ti 六路实测结论。

Windows worker 启动后先等待父进程 gate；父进程加入 kill-on-close Job Object 后才允许解码。取消/超时终止整棵自有进程树。租约回收核对 PID 创建身份，不能仅凭心跳超时认定进程已经退出。

分组压缩获得独占租约时，其 NVENC FFmpeg 也纳入公共 ProcessTree 管理，避免服务退出后遗留转码子进程；转码判定与低码率原样发布规则保持原有实现。

## 已完成的验证与未验证项

- 最小编译检查通过。最终全量筛选回归 117 项通过；随后增加的五角色删除、通知溢出与分组迁移拒绝残留共 3 项也通过相关补测。合计 120 个不同筛选测试已验证。
- 公共血缘 12 项通过；既有压缩回归 38 项通过。
- 临时 SQLite 迁移匹配当前模型；旧数据拒绝升级、新数据拒绝降级，其他表和公共血缘保留。跨组组合外键、核心 Connection 摘要读取、独立 hash/标签和类型专属超参 digest 均有测试。
- supervisor 模拟验证两个组共六条任务重叠，第七条等待。真实 Windows 合成父子进程验证 Job Object 子进程终止；临时合成文件验证非递归目录通知与同步取消后 watcher 线程退出。没有使用真实视频。
- 隔离 Flask 浏览器测试显示六个进度卡，组切换后片段仍指向该组；暂停/手动刷新、请求故障保留内容正常。390px 视口没有全页横向溢出。临时服务已关闭。

**尚未实测**：隔离 PostgreSQL 的跨进程 advisory lock/SKIP LOCKED 与清理 SQL 实际执行；3060 Ti 的真实 1/2/4/6 路模型负载、OOM、吞吐及压缩互斥联调。没有提供或使用隔离 PostgreSQL 目标，也没有枚举真实组内或组外媒体来代替授权样本。

真实 GPU 基准工具只接受已配置组与明确文件。先停止相关服务，从 automation-server/ 执行：

```text
mamba run -n autoflow python -m video_filter.diagnostics
mamba run -n autoflow python -m video_filter.smoke --group example_group --manifest video_filter/weights/manifest.json --files <group_allowed_file> --seconds 10
mamba run -n autoflow python -m video_filter.tests.gpu_validation --group example_group --files <group_allowed_file_1> <group_allowed_file_2> --labels 1 0 --concurrency 2 --services-stopped
```

基准写入临时数据库/状态目录，不操作原文件；请求并发可选 1/2/4/6，仍受相同显存准入限制，报告实际重叠数和吞吐。liked_source 及配置外路径会拒绝采样。此工具不会训练或激活生产模型。

## 后续 GPU 执行调整（2026-10-05）

视频解码已切换 NVIDIA NVDEC。FFmpeg 使用输入流的明确 CUVID 解码器与 VIDEO_FILTER_DEVICE 对应的 CUDA 序号，继承 CUDA_VISIBLE_DEVICES；缩放、裁剪、RGB 转换保持原预处理，帧仍通过主机内存交给特征模型。音频解码与 openSMILE 仍使用 CPU。元数据保存视频编码、解码后端与设备，worker 启动日志标明 NVIDIA NVDEC。

需要本地 FFmpeg 包含对应的 `_cuvid` 解码器、可用 NVIDIA 驱动及支持该编码/位深/色度格式的显卡。当前目录提取要求 `VIDEO_FILTER_DEVICE=cuda:0`（或其他有效 CUDA 序号）。不支持的编码返回 nvdec_codec_unsupported；驱动、硬件格式或解码器失败保留异常堆栈和源文件，不回退软件解码。diagnostics 的环境报告现在包含可用 CUVID 解码器，组内解码诊断与 smoke 使用同一 NVDEC 路径。

MIL 预测已切换独立 PyTorch worker，加载数据库中已有的数值权重，在 eval/inference_mode 下完成 GPU 标准化、编码、注意力池化与分类。所有窗口参与，默认每块最多 512 个窗口；关闭 TF32 近似。Flask 仍不导入 torch。现有 MIL 数值模型可以直接复用，无需因执行后端变化重训；NumPy 实现保留为训练输出校验与测试参考。

predict/classify 任务只要包含 MIL（即使选择 LR 搬运），就先申请共享 GPU 租约；与提取合计受同一容量/显存约束，并等待独占训练/压缩。预测 worker 使用已有超时、Job Object、取消、日志转发与临时文件清理。配置 CUDA 时失败不回退 CPU；显式 CPU 仅用于隔离数值测试。

搬运前先核验源 hash、提交双预测，再登记操作；后续门禁/恢复复用该批预测并校验模型、摘要、标签版本与阈值，不重新占用 GPU。GPU 失败或推理期间源消失不会提交部分双预测或开始搬运。工作台显示“GPU 共享准入（提取/预测）”，日志与实时进度显示 MIL PyTorch 推理阶段。

本轮没有修改数据库 schema、真实配置或依赖。设置已有 `VIDEO_FILTER_DEVICE=cuda:0` 后，停止并重新启动服务即可加载新代码；旧多组改造仍按前述 SQL/迁移流程首次启用。每次 MIL 预测目前会启动独立 worker，存在进程/CUDA 初始化开销；尚未进行真实媒体目录的吞吐比较。

验证：完整筛选回归 130 项通过；随后新增搬运门禁复用及 GPU 失败/源删除测试，双模型、调度、GPU 传输与页面补测 42 项通过，合计 132 个不同筛选测试已验证。临时生成的 H.264 视频通过真实 NVDEC 连续帧检查；HEVC 10-bit、MPEG-2 视频通过连续帧与短尾抽帧检查；H.264 音轨输出 16000 Hz 样本。真实 CUDA MIL worker 对 519 窗口合成特征的结果与 NumPy 参考绝对误差小于 1e-5。未读取用户媒体目录，也未运行生产服务/数据库操作。PostgreSQL 跨进程及六路真实媒体负载仍未实测。

逻辑回归本轮保持 sklearn/CPU。仅推理改为 GPU 的成本低，可直接使用现有标准化、系数与截距；完整训练改为 PyTorch GPU 的实现成本中等：可沿用摘要与数值参数存储，但需要重新验证求解器、类别权重、C 与 L2 正则的等价关系、收敛条件及激活/评估结果，并重新训练。cuML 路线会增加 RAPIDS 环境兼容负担，原生 Windows 通常需要改为 WSL2/Linux。GPU 不会天然提高准确率，是否提速应在实际样本规模下测量。
