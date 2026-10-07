# Automation Flow 项目

## 项目目标

本仓库用于实现仅在本地运行的自动化流水线。主服务位于 `automation-server/`，使用 Flask、Flask-SQLAlchemy 和 Flask-APScheduler；各条业务流水线是该目录下彼此独立的 Python 子包。

## 本地视频偏好筛选

`automation-server/video_filter/` 提供五类目录追踪、数据库特征摘要、删除反馈、个人二分类训练与可恢复分类发布。DINOv2、VideoMAE、BEATs 和 openSMILE 全部本地运行，默认关闭。配置、接口、环境准备及实际验证见 [实施记录](plan/implementation.md)；只有配置路径、完成数据库迁移并满足摘要/训练门槛后才能启用自动分类。

分组运行时，NVENC 转码与特征提取、MIL 预测共享 GPU，按驱动空闲显存扣除未兑现预留后准入；每张物理显卡最多一个转码任务。`VIDEO_COMPRESSION_GPU_PEAK_MIB` 设置转码预计新增显存峰值，默认 1024 MiB；安全余量沿用 `VIDEO_FILTER_GPU_SAFETY_MIB`。`VIDEO_COMPRESSION_GPU_WAIT_SECONDS` 设置显存或编码名额不足时的最长等待，默认 1800 秒，与 FFmpeg 执行超时分开。等待超时会记录失败并保留源文件。

当前没有转码运行时，后到的提取/预测任务会为最早等待的转码预留预算，再使用剩余显存补位，避免连续补位造成转码饥饿；多个等待转码只预留一份预算，已过期的请求不再占预留。

只有实际转码期间持有 GPU 租约，目录扫描、CPU 解码校验、复制、发布和源文件清理不持有租约；MIL 训练继续独占。共享压缩在数据库中使用兼容的 `extract_shared` 模式，通过 `memory_observation.workload_type=compression` 区分，工作台单独显示压缩运行/等待数量。日志记录压缩等待准入、获得资源和释放资源；显存未查询与查询失败分别显示。

此次调整无需新增迁移，但依赖已经部署的显存预算字段迁移。更新后需先停止旧服务、等待其 FFmpeg 退出，再启动新版本；旧版的 `exclusive_compression` 租约仍按原有互斥规则处理，不能在旧任务存活时强行清除。实际共享吞吐与显存峰值需在本机运行后验证，1024 MiB 是初始预算而非硬件实测保证。

## 日志与排障

服务启动时会创建按大小轮转的 UTF-8 日志文件（单文件 10 MiB，保留 10 个备份）：

- 框架日志：`automation-server/logs/runtime.log` 与 `error.log`；
- 子项目日志：`automation-server/<子项目>/logs/runtime.log` 与 `error.log`。

`runtime.log` 保存 INFO 及以上的完整时间线，`error.log` 只保存 ERROR/CRITICAL。子项目异常只写入该子项目目录，不会写入框架错误日志；Python 异常记录包含完整 traceback。日志目录不可创建或文件不可打开时，服务会拒绝启动，避免在无持久日志的状态下运行。

排查问题时先查看对应子项目的 `error.log`，再结合该目录的 `runtime.log` 查看异常前后的任务状态。日志文件和轮转备份均已加入 `.gitignore`，不得提交到仓库。

`video_filter/logs/performance.log` 单独保存 JSON Lines 性能记录，沿用 10 MiB/10 个备份的轮转规则，不写入控制台和运行日志。记录包含 GPU 空闲/已用显存与利用率、准入拒绝原因、未兑现预留与显存归属状态、任务排队及执行耗时、控制轮耗时、模型加载和各模态耗时、PyTorch 显存占用与峰值。可按 `task_id`、`dataset_group_id`、`event` 关联记录；时间戳使用 UTC，记录不包含视频名称、路径或原始音频。性能记录在服务重新启动后生效。

特征提取按窗口组成有限批次，DINO 图像、VideoMAE 视频片段、BEATs 音频段的最大推理批量分别由 `VIDEO_FILTER_DINO_BATCH_SIZE=16`、`VIDEO_FILTER_VIDEOMAE_BATCH_SIZE=4`、`VIDEO_FILTER_BEATS_BATCH_SIZE=8` 控制（1–64）。这些配置优先于旧的 `VIDEO_FILTER_BATCH_SIZE`；BEATs 仅合并相同长度的音频段，短尾保留原来的处理方式。发生批量 CUDA OOM 时减半重试，单项仍 OOM 时按既有任务失败机制处理。采样位置、每个窗口的特征维度和数据库摘要格式不变，已有摘要继续复用。

分组任务启动后先在 CPU 校验源文件、加载模型；每批 NVDEC 解码/深度推理才向父进程申请 GPU 租约，批次结束后模型移回 CPU、清空 CUDA 缓存再释放。音频连续解码和 eGeMAPS 在 GPU 租约外执行。`VIDEO_FILTER_EXTRACT_CONCURRENCY` 限制整条视频处理链数量，GPU 阶段另外受共享配额、显存预算和训练独占等待控制；初始显存预算沿用原配置，按模型与批量分别记录实际显存观测和 OOM 反馈。MIL 在 CPU 数据读取和标准化完成后才申请训练独占，GPU 计算完成后释放，再序列化和入库；预测也按实际推理阶段申请共享租约。

性能日志新增 `gpu_phase_started`（`wait_seconds`）、`gpu_phase_finished`（`hold_seconds`）、`inference_batch`（实际批量）、`inference_batch_reduced`（OOM 降批）和 `audio_batch_prepared`。模型移回 CPU 会增加 PCIe 搬运开销；需根据这些记录判断整体吞吐提升，再调整批量。错误阶段保持租约直到子进程树退出，避免异常堆栈仍引用 CUDA 张量时提前放行其他任务。上述调整需重启服务生效，无需迁移或清理已有数据库。

`VIDEO_FILTER_PERSISTENT_WORKERS=true` 默认启用按需创建的常驻提取/预测进程：完成一个视频后，下一任务复用进程和 CPU 模型缓存，省去重复 Python/PyTorch 初始化和权重读取。提取进程数量受全局提取并发限制，MIL 预测进程最多 2 个；每个进程同时执行一个任务，MIL 训练仍使用独立进程。冻结模型按特征签名和文件身份复用，配置/预处理文件变化会重新校验；特征版本变化使用新进程。MIL 缓存按组、代次及模型 SHA256 区分，训练更新模型后自动使用新版本。视频数据、任务日志及摘要输出每次独立，GPU 仍按实际计算阶段申请，空闲时仅保留 CPU 模型和 CUDA 运行时上下文。

每个进程的 `VIDEO_FILTER_WORKER_MODEL_CACHE_MIB=768` 限制已缓存模型的参数/缓冲区估算量，超限按 LRU 淘汰；该值不等于进程总内存上限，Python/模型构造、当前计算批次另占内存。总缓存预算随常驻进程数量增加，可调小但可能增加权重重读。空闲约 `VIDEO_FILTER_WORKER_IDLE_SECONDS=120` 秒后由控制轮回收，成功执行 `VIDEO_FILTER_WORKER_MAX_TASKS=100` 次后重建；超时、取消、输出校验失败或模型异常会终止完整进程树，确认退出后释放资源，下个任务重建。服务关闭也会回收池内进程。性能日志用 `worker_started.reused`、`worker_pid`、`model_cache.hit/cached_mib`、`worker_retired` 判断复用与回收。设为 `false` 可恢复每任务独立进程；配置需重启生效，不影响数据库摘要格式。

任务与分组扫描完成时会通知原有调度器尽快补位；两秒定期控制仍作为兜底，不新增并行控制任务。同一控制轮复用短时驱动采样，各新任务仍分别预留预算。音频使用一个连续 FFmpeg 进程，按原来的 10 秒边界切分，在同一窗口内顺序计算 BEATs 与 eGeMAPS；原始 PCM 只在内存中保留当前窗口，无音轨及空尾窗口仍使用明确缺失掩码。连续路径不需要缓存整个音轨，`VIDEO_FILTER_AUDIO_CACHE_MIB` 保留供兼容的逐窗口解码路径使用。
