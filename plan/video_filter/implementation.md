# video_filter 实施记录

## 实施范围

2026-10-04：P1–P7 的代码已接通，不再按阶段停工。需求基线为 [research.md](research.md)，原实施计划为 [codingplan.md](codingplan.md)。2026-10-05 的双模型与用户反馈准确率升级见 [dual-classifiers.md](dual-classifiers.md)。长期特征和个人分类器数值参数均存数据库；未执行生产迁移、正式目录搬运/删除、真实定时任务或 Git 提交。

| 阶段 | 主要代码 | 行为 |
| --- | --- | --- |
| P1 | `configuration.py`、`models/`、`feature_store.py` | 配置代次、持久身份、原子数据库摘要 |
| P2 | `tracking.py`、`feedback.py` | 稳定扫描、移动/副本对账、版本反馈与重试日志 |
| P3 | 公共 `media_lineage/`、压缩服务及调度 | 压缩前登记、发布后关联、清理前验证、恢复保护 |
| P4 | `media.py`、`features/`、`extraction.py`、`worker.py` | 四模型本地提取、独立进程超时、逐阶段释放 GPU |
| P5 | `learning.py`、`mil.py`、`training_worker.py` | 同签名 Asset 级样本、独立 LogisticRegression/MIL 训练、验证与按模型类型激活 |
| P6 | `transfer.py` | 已提交摘要门槛、原名发布、禁止覆盖、持久搬运与硬链接恢复证据 |
| P7 | `runtime.py`、`tasks.py`、`apis/control.py` | 控制轮次、任务认领、恢复、自动入队、状态/反馈/重试接口 |

## 数据存储与身份

视频不进入数据库。`video_filter_feature_bundle.arrays_blob` 保存压缩数值 NPZ（PostgreSQL `BYTEA`）；`manifest` 保存 JSON，包括源 hash、四路签名、窗口覆盖、校验与有效性。窗口原始特征、均值/标准差和有效性掩码同事务提交为 `ready`。不保存截图、原音频或转录。逻辑回归使用每路均值、标准差和逐维覆盖率；MIL 使用同一摘要中的逐窗口特征与掩码，整部视频只提供一个标签。切换分类器不改变提取签名，不要求重新分析视频。

`FeatureStore.require_ready()` 从独立连接读取已提交数据并验证 checksum、维度、窗口覆盖、源身份，未提交的 ORM flush 不能通过分类门槛。相同版本与签名的摘要不可覆盖。个人分类器参数保存到 `ModelRun.model_blob`：逻辑回归使用数值 JSON；MIL 使用压缩数值 NPZ，包含标准化参数、网络权重和架构版本，校验归档尺寸、形状与有限值，不使用 pickle。

状态目录只用于临时 worker 传输、控制/GPU 锁与未提交反馈日志。worker 临时 NPZ 入库后清理；它不承担长期训练数据存储。原视频删除后，数据库摘要继续可训练；没有及时提取就已被删除的视频只有标签/追踪记录，不能补造训练特征。

扫描先关联所有新位置，再处理旧位置缺失。同字节全量 SHA-256 对应同 Variant；可信压缩血缘把不同 hash 的成品关联到同 Asset。已登记且文件身份、大小和修改时间未变的文件复用此前 hash，提取与清理仍重新核验完整 hash。首次扫描/配置变化建立新基线；目录不可访问、扫描途中变化、未消费血缘、进行中操作或存活副本均阻止自动负反馈。

目录 1 新迁入产生 `1`；开启删除反馈后，完整扫描发现全部受管位置持续缺失并超过等待期才产生 `0`。观测范围只包含五类显式配置的目录，不自动加入独立压缩源目录；没有血缘记录的范围外移动无法仅凭文件消失识别。目录 6 无确认记录的视频保持未知，即使有压缩血缘也不会自动变成正样本，不能凭同名合并或猜测喜好。

每次反馈增加 `label_revision`，保留旧事件和摘要。只有训练/验证快照包含该资产的活动模型才退休；新视频的首次用户反馈不使既有模型失效。反馈日志先 fsync 落盘再提交数据库；失败可重放，旧反馈不能覆盖新版本。已有更新标签版本的过期日志保留为 `.superseded` 记录；其余残留冲突会暂停新分类发布，需要核对。程序预测从不写入训练标签。

## 压缩目录与相对路径

压缩流程是目录 A → 目录 B。`VIDEO_COMPRESSION_SOURCE_DIR` 是 A，`VIDEO_COMPRESSION_OUTPUT_DIR` 是 B；`VIDEO_FILTER_CONFIRMED_LIKE_DIR` 可以是独立的目录 C，只有 C 中的视频提供目录确认的正标签。A 与 B 必须分开，独立的 A 也不能与其他角色或状态目录嵌套。`compressed_like` 保留为 B 的兼容角色名，不表示其中所有视频已确认喜欢。

可信血缘把 A 的源版本与 B 中 hash 不同的成品关联到同一 Asset。计划执行和直接压缩入口均按实际源位置记录角色；源不在确认喜欢目录时不会伪造 `confirmed_like` 证据。未知视频的源与成品都保持未知，已有正负标签及标签版本继续保留，不因重编码改变。根据用户要求，筛选程序不扫描独立 A、不读取其中视频的 hash、不生成源视频摘要或分类任务；A 中临时文件变化或消失不会影响五类目录的扫描完整性。压缩项目负责登记源身份、发布及源清理，筛选只消费其已提交血缘；进行中的操作阻止相关资产的删除反馈，成品发布后继承原资产身份。此调整不新增数据库表、Location 角色或迁移。

所有配置相对路径固定以 `automation-server/`（`env.py` 所在目录）为基准，与启动命令的当前工作目录无关。示例：

```dotenv
VIDEO_FILTER_CONFIRMED_LIKE_DIR=<directory_c>
VIDEO_COMPRESSION_SOURCE_DIR=<directory_a>
VIDEO_COMPRESSION_OUTPUT_DIR=<directory_b>
VIDEO_FILTER_STATE_DIR=video_filter/state
VIDEO_FILTER_MODEL_MANIFEST=video_filter/weights/manifest.json
VIDEO_FILTER_LINEAGE_ENABLED=true
```

以上两个相对路径分别解析为 `automation-server/video_filter/state/` 和 `automation-server/video_filter/weights/manifest.json`。主服务仍建议在 `automation-server/` 下执行 `mamba run -n autoflow python main.py`。状态目录保存锁、提取临时传输和反馈重试记录；manifest 指向本地提取模型清单，个人分类器参数继续存数据库。

2026-10-04：根据用户明确“仅确认喜欢目录具有正标签”，移除 A 必须等于 C 的旧校验，并补齐未知源、最新负标签、直接压缩入口及进行中血缘保护的回归测试；进一步按用户要求移除独立压缩源监控，补充不枚举、不 hash、不登记源文件及源目录消失不影响扫描的验证。已使用本机配置只读验证配置可解析、A/C 独立及本地 manifest 存在；未运行真实调度器、修改真实视频或数据库。

目录标签修复通过筛选 74 项、血缘 12 项、压缩回归 38 项，以及 Python 语法检查。取消源监控后，筛选 75 项、血缘 12 项及 Python 语法检查通过，验证源目录不枚举、不 hash、不登记，变化或消失不影响扫描。重启主服务后加载修复；移除扫描配置中的源目录字段会生成新的配置快照，首次完整扫描建立基线，该轮不产生删除反馈。

## 模型与环境

当前本机已准备：Python 3.14.4、NumPy 2.4.3、PyTorch 2.11.0+cu128、torchvision/torchaudio 0.26.0/2.11.0+cu128、timm 1.0.30、Transformers 5.18.0、openSMILE 2.6.0、scikit-learn 1.9.1、Pillow 12.3.0。FFmpeg/FFprobe 已从既有配置定位。CUDA 12.8 在 RTX 3060 Ti 8GB 上可用。

四路输出维度为 DINOv2 384、VideoMAE 384、BEATs 768、eGeMAPSv02 88。采用完整连续 10 秒窗口；DINO 每窗口取三帧，VideoMAE 使用窗口中央连续两秒、8 FPS、16 帧；BEATs 使用 16kHz 单声道音频，分成最长 5 秒片段；声学统计使用整个窗口并标记无声/无效音高等值。无音轨具有明确掩码，不能把音频提取失败当成无音轨。

DINO 官方 checkpoint 使用 timm 的权重转换与位置编码适配。VideoMAE 小模型的 HF 配置错误声明 16 个注意力头，按官方 ViT-S 定义固定为 6 头，并将此修正纳入签名；兼容 Transformers 5 的 bias 名称。BEATs 使用预训练特征接口，不读取分类概率。openSMILE 固定完整配置树 hash。

Windows 现有 MKL 与 torch OpenMP 存在冲突，worker 子进程使用受支持的 `MKL_THREADING_LAYER=SEQUENTIAL`；不设置忽略重复运行库的开关。Pillow 原 Conda 二进制在 torch 同进程内加载失败，已安装可用的 12.3.0 wheel。环境原有 ChromaDB 缺失 onnxruntime，与本项目无关，本次未扩展修复。

权重已经下载到被忽略的 `automation-server/video_filter/weights/`。BEATs 官方 Azure 地址禁止公开访问，使用固定提交的 `lpepino/beats_ckpts` 镜像，核对仓库 LFS SHA-256。BEATs 源码固定 upstream commit；本地 manifest 固定全部产物 hash。推理没有联网下载回退。

新机器可以显式运行准备命令（从 `automation-server/`，代理参数填写本机值）：

```text
mamba run -n autoflow python -m pip install torch==2.11.0 torchvision==0.26.0 torchaudio==2.11.0 --index-url https://download.pytorch.org/whl/cu128 --proxy <proxy_url>
mamba run -n autoflow python -m pip install -r video_filter/requirements-local.txt --proxy <proxy_url>
mamba run -n autoflow python -m video_filter.provision --directory video_filter/weights --proxy <proxy_url>
mamba run -n autoflow python -m video_filter.diagnostics
```

## 训练与搬运门槛

只选择最新明确 `0/1` 标签及同特征签名的有效摘要，每 Asset 一份代表特征；源/压缩版不会跨验证集合。至少每类 10 个资产才训练，固定随机种子，按资产分层划分 70% 训练、30% 验证；标准化仅拟合训练集。阈值在验证集选择，指标明确标识为验证集结果，不能视为独立测试准确率。balanced accuracy 和 ROC AUC 均至少 0.6 才激活；样本少或验证不通过时不搬运。

激活前核对所有标签版本和数据快照；训练/验证样本的标签变化使对应模型退休。发布需固定配置、标签版本、模型、特征签名、源文件快照和已提交摘要。两套 Prediction 先入库，采用环境变量所选模型建立 TransferOperation，然后独占暂存复制、完整 hash 核验、无覆盖硬链接发布；暂存硬链接作为归属证据保留到清理结束。目标 Location 与血缘先提交，再重新核验源/目标后清理源。目标同名、用户更换源、目标消失或来源不明时保留源并报告冲突。

压缩任务在准备前持久登记源快照，成品发布后保存血缘，再清理源。普通执行、验证恢复、清理恢复、处理中恢复和直接服务入口均接入公共契约。旧在途任务缺少血缘时安全失败保源；禁用筛选集成时保留压缩原行为与 5 Mbps 规则。压缩与提取共用 GPU 锁，采取串行资源使用。

## 运行控制与接口

2026-10-05 新增 Flask/Jinja 本地工作台：`/dashboard/` 为服务导航，`/video_filter/dashboard/` 显示摘要覆盖、当前任务阶段、训练轮次及两类模型的用户反馈准确率，每 5 秒只读刷新。公共布局通过显式服务注册支持后续扩展，不增加依赖或数据库迁移；使用方式与验证记录见 [dashboard.md](dashboard.md)。

唯一调度 ID 为 `video_filter_process_one`，30 秒触发，`max_instances=1`、coalesce、120 秒 misfire grace。一次控制轮次获取跨进程控制锁，处理一个待恢复搬运、重放反馈、完整对账，再认领一个任务；数据库条件更新与 claim token 防重复完成。提取独立进程有硬超时，重型模型不进入 Flask 进程。

自动流水线逐轮推进摘要、独立训练和预测。数据足够时分别训练两套模型；有活动模型时优先为已具备摘要的未分类或预测目录中的未知标签视频记录结果，再继续其他视频的摘要，避免长队列延迟用户反馈评估。搬运开关关闭时仍可保存预测；开启后仅采用所选模型搬运未分类视频。失败的相同输入不会每轮无限重试；最多三次显式任务重试。暂存归属不明或冲突操作必须核对，重试接口不能绕过冲突或过期配置。

| 接口 | 请求/结果 |
| --- | --- |
| `GET /video_filter/status` | 样本、摘要、任务、位置/搬运冲突、所选模型、两套活动模型及实际准确率 |
| `GET /video_filter/metrics` | 两类模型及各版本的用户反馈准确率、共同样本比较、混淆计数、置信区间 |
| `GET /video_filter/assets` | 分页列出资产/版本 ID、最新标签版本、文件名与角色，供反馈核对；不读取数值摘要 |
| `POST /video_filter/scan` | `{}`，扫描入队 |
| `POST /video_filter/extract` | `{"variant_id":"<uuid>"}`，摘要提取入队 |
| `POST /video_filter/train` | `{}` 训练所选类型，或 `{"model_type":"mil"}` / `{"model_type":"logistic_regression"}` 分别入队 |
| `POST /video_filter/predict` | `{"variant_id":"<uuid>"}`，两套活动模型预测入库，不搬运 |
| `POST /video_filter/classify` | `{"variant_id":"<uuid>"}`，完整摘要/模型门槛校验后入队 |
| `GET /video_filter/tasks/<id>` | 脱敏状态、尝试次数、错误代码 |
| `POST /video_filter/tasks/<id>/retry` | 当前配置下无冲突的失败任务重新入队 |
| `POST /video_filter/feedback` | asset_id、label（整数 0/1）、expected_revision、event_key（SHA-256 幂等键） |

接口不接受任意文件目标路径。视频发现和处理范围来自 `EnvConfig` 的五类目录。公开响应/运行日志只输出技术状态、ID 与错误代码，不输出完整路径、向量或内容描述。

## 运行日志与排错

`video_filter` 已接入主服务的统一日志配置；通过 `main.py` 启动时自动创建以下文件，同时输出到控制台，不需要新增环境变量：

- [runtime.log](../automation-server/video_filter/logs/runtime.log)：INFO 及以上，包含日常进度、等待原因、警告和错误。
- [error.log](../automation-server/video_filter/logs/error.log)：仅 ERROR/CRITICAL，集中保存失败记录和完整 Python 堆栈、异常因果链。

两个文件均使用 UTF-8，每个文件达到 10 MiB 时轮转，最多保留 10 份备份。日志目录已被 Git 忽略。错误同时出现在运行日志中，便于结合前后阶段排查。

运行日志覆盖调度注册（实际间隔仍为 30 秒）、目录枚举及对账结果、配置基线、文件稳定等待、正负反馈、样本不足原因、任务登记/开始/结束及耗时。提取记录媒体时长、窗口数、音轨状态、四路模型加载和释放、实际窗口完成数/百分比、阶段耗时及 PyTorch allocated 显存峰值；窗口进度在首尾及完成窗口后至少间隔 5 秒输出一次。训练记录样本数量、特征维度、训练/验证集大小、最终求解迭代数、验证指标、阈值、数据库模型 ID、参数字节数和激活结果。分类记录分数、阈值、目标角色，以及暂存核验、发布和源清理的提交阶段。

训练摘要读取、输入快照核对和求解拟合阶段每 15 秒输出仍在运行的心跳及耗时；求解器不提供逐迭代回调，因此心跳不是训练百分比。提取子进程通过实时 JSON 日志通道交给主进程落盘，避免多个进程同时轮转同一文件；失败时保留子进程原始堆栈并链接主进程调用堆栈。FFmpeg/FFprobe 失败附带退出码和 stderr；超时保留 TimeoutExpired 异常链。日志脱敏配置路径和凭据，SQLAlchemy 异常隐藏绑定参数，避免输出数值特征或模型参数正文。

从仓库根目录可以在 PowerShell 中持续查看：

```powershell
Get-Content automation-server/video_filter/logs/runtime.log -Tail 80 -Wait
Get-Content automation-server/video_filter/logs/error.log -Tail 80 -Wait
```

使用 `task_id` 关联任务生命周期和提取日志，使用 `variant_id`、`asset_id`、`model_id`、`operation_id` 对应数据库记录。目录不可访问或扫描途中变化会明确记录暂停删除判定；日志补齐未修改调度间隔、任务状态、数据结构或分类搬运门槛。

## 验证与启用条件

2026-10-04 自动摘要入队修复：PostgreSQL 拒绝 `SELECT DISTINCT id ORDER BY created_at`，因为排序列不在投影中。候选查询改为同时选择 `id`、`created_at`，仍通过 `scalars()` 获取 ID；多位置副本继续去重，保持创建时间顺序与 20 个候选的上限。新增回归覆盖去重、已保存同签名摘要及缺失位置的排除、候选顺序/数量，并对实际语句检查 PostgreSQL 的排序投影规则。在已配置 PostgreSQL 上以只读事务执行修正查询的 `EXPLAIN` 已通过；未执行 `EXPLAIN ANALYZE`、入队或任何数据写入。本次无需迁移，主服务重启后加载查询修复。

当前已通过筛选 60 项、血缘 10 项、压缩既有回归 38 项。测试使用临时文件 SQLite、隔离 Flask app/scheduler、临时目录和 mock；不启动 scheduler 或调用生产数据库。三段新增迁移链在隔离数据库中验证模型一致性与升级/降级保护。PostgreSQL 专属锁已有调用/编译检查，真实并发事务尚未联调。

日志补齐后筛选测试共 67 项通过，其中新增 7 项覆盖项目日志分流、完整异常链、SQL 绑定参数隐藏、真实隔离子进程失败堆栈、退出前实时转发、超时终止与训练心跳/指标。共享日志及异常边界测试 15 项中 14 项通过；既有 `test_image_project_paths_are_not_registered_for_redaction` 的期望与 HEAD 中已经登记图片路径脱敏的配置不一致，本次未修改该图片配置或测试。语法检查与日志专项验证不连接生产数据库或启动真实调度器。

两份用户样本已完成整片独立 GPU worker 提取、临时数据库提交与重新读取，原视频保持不变；真实数据库没有写入样本。最大阶段 PyTorch allocated 显存约 391 MiB，该指标不含驱动、解码器及其他应用显存。模型按阶段串行，无需四模型同时驻留。

| 测试样本角色 | 时长 | 窗口数 | 数据库摘要 | 四路阶段耗时之和 |
| --- | --- | --- | --- | --- |
| 不喜欢 | 183 秒 | 19 | 130,420 字节（约 127 KiB） | 约 64 秒 |
| 喜欢 | 68.766667 秒 | 7 | 57,989 字节（约 57 KiB） | 约 33 秒 |

耗时包含模型加载和各路解码，不含全部子进程启动及摘要入库开销；不能直接外推长视频吞吐。DINO/VideoMAE/BEATs 的阶段显存峰值分别约 97/135/391 MiB。整片测试发现并修正末尾单帧采样丢帧；此前还修正权重适配、Pillow 与声学只读数组问题。训练正确返回每类至少 10 个资产的不足原因，没有激活分类模型。两个样本适合验证管线，不能证明偏好泛化准确率。

实际验证命令（从 `automation-server/`）：

```text
mamba run -n autoflow python -m unittest discover -s video_filter/tests -t .
mamba run -n autoflow python -m unittest discover -s media_lineage/tests -t .
mamba run -n autoflow python -m video_filter.tests.compression_regression
mamba run -n autoflow python -m compileall -q video_filter media_lineage video_compression env.py main.py migrations
mamba run -n autoflow python -m video_filter.smoke --manifest video_filter/weights/manifest.json --sample-directory <sample_dir> --seconds 10
mamba run -n autoflow python -m video_filter.tests.gpu_validation --manifest video_filter/weights/manifest.json --sample-directory <sample_dir> --labels 0 1
```

正式启用仍需要用户确定五类目录和独立状态目录，在 `.env` 指定本地 manifest，确认数据库目标后执行 Alembic 升级。此处没有替用户执行真实迁移。全部功能开关在 `.env.example` 默认 false。

建议先保持搬运/删除反馈关闭，启用扫描与摘要并检查覆盖；使用反馈或安全的受管目录行为积累负样本，再开启删除反馈与压缩血缘，训练模型通过门槛后开启分类搬运。压缩源可独立于目录 1，压缩成品只继承已有确认标签。新配置自动建立新基线，不复用旧路径任务；数据和模型不足时状态接口明确显示未启用自动分类。
