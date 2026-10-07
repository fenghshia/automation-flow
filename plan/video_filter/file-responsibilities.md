# media_lineage 与 video_filter 文件职责汇总

核对日期：2026-10-06。依据当前工作区源码，包含尚未提交的改动与新增文件，已覆盖批量推理、按 GPU 阶段申请资源、常驻 worker 和 CPU 模型缓存。

本文用于审查和重新分类文件，描述的是当前职责及调用关系；本次没有移动代码、修改配置或处理业务数据。

## 1. 范围与阅读约定

- 完整列出 `automation-server/media_lineage/`、`automation-server/video_filter/` 的业务代码、模板、工具和测试文件。
- 补充它们实际依赖的仓库其他位置，以及 Git 忽略的模型、日志、状态、压缩缓存和样例媒体。
- 不展开 `.env` 或分组 JSON 中配置的媒体目录、外部工具目录及其他自定义目录，不记录真实配置值、组 ID、视频名称或日志内容。
- 下文运行时路径以源码默认的 `automation-server/video_filter/state/`、`weights/` 为例；配置可改变其实际位置。`<group-id>`、`<epoch>`、`<task-id>` 是占位符。
- “常规入口”指 `main.py` 启动后的注册与调度链；“手动工具”只在明确调用时执行；测试不会自动进入业务流程。

持久数据主要在数据库。`state/` 同时包含恢复记录、锁和临时传输文件；`weights/` 包含冻结的预训练模型及第三方模型代码；`logs/` 用于运行诊断。这三者的用途不能互换。

## 2. 当前调用与数据关系

```text
main.py → video_filter.register_video_filter()
    ├─ 注册 models、控制 API、Jinja 页面
    └─ runtime.register_schedules() → supervisor.tick()
           ├─ 分组隔离、通知、配置版本、扫描与流转对账
           │     → tracking / identity / feedback
           ├─ runtime 发现工作 → tasks 入队与认领
           └─ 按容量派发任务 → runtime.execute()
                 ├─ extract → worker_client → worker / persistent_worker
                 │      → extraction → batched_extraction
                 │      → media + features/* → feature_store → 数据库摘要
                 ├─ train → learning
                 │      ├─ LR：CPU 上拟合
                 │      └─ MIL：training_worker → mil → GPU 拟合
                 │      → 数据库模型参数、超参、样本快照、验证指标
                 ├─ predict → prediction → LR + MIL 独立预测 → 数据库
                 └─ classify → transfer → 按选定模型安全搬运

删除/确认喜欢 → tracking → feedback → evaluation
    → 数据库标签、反馈历史、预测与实际结果关联、实际准确率

video_compression → media_lineage.integration
    ├─ workflows：读取已发布的分组压缩源/输出绑定
    ├─ service：记录 planned → destination_verified → published → source_cleaned
    └─ resources + process_tree：GPU 编码阶段的预算准入和进程生命周期

video_filter → media_lineage
    ├─ tracking 消费压缩血缘，关联重编码前后的内容身份
    ├─ transfer 发布自身的流转血缘
    └─ supervisor 使用共享 GPU 租约及进程管理能力
```

不同组各有标签、特征、模型、超参、预测与评价数据；GPU 准入和 worker 池管理物理资源。分组独立不代表每组分别获得一套 GPU 并发额度。

`liked_source` 参与文件流转追踪和压缩绑定，但不执行视频采样；产物进入 `liked` 后才由提取流程处理。不能只凭源文件暂时消失就生成不喜欢标签，需要经过目录对账、移动/压缩证据检查和缺失确认。

## 3. media_lineage：公共能力及压缩适配

目录：[automation-server/media_lineage/](../../automation-server/media_lineage/)。当前包名表达“媒体血缘”，实际还承载 GPU 与进程基础设施。

| 文件 | 当前归属 | 具体作用与使用方 |
| --- | --- | --- |
| [__init__.py](../../automation-server/media_lineage/__init__.py) | 包入口 | 无应用初始化副作用，`__all__` 为空；具体能力由调用方显式导入子模块。 |
| [models.py](../../automation-server/media_lineage/models.py) | 公共持久化 | 定义 `LineageEvent`、`WorkflowBinding`、`ResourceLease` 三种 ORM 模型；事件仅追加，保存流转证据、压缩绑定和 GPU 租约。共享 `app.db`。 |
| [files.py](../../automation-server/media_lineage/files.py) | 文件身份 | 拒绝符号链接/reparse point，生成规范化路径摘要、文件快照及稳定读取的 SHA-256；两个媒体流程使用。 |
| [service.py](../../automation-server/media_lineage/service.py) | 血缘业务 | 校验与幂等追加事件、游标读取、检查源身份、验证输出、记录发布和源清理；处理事件顺序与 PostgreSQL 提交顺序。调用方负责相应事务提交。 |
| [workflows.py](../../automation-server/media_lineage/workflows.py) | 公共流程绑定 | 发布分组 `liked_source → liked` 绑定，提供压缩执行上下文和目录匹配证据。筛选流程写绑定，压缩流程读绑定，避免直接依赖筛选内部表。 |
| [integration.py](../../automation-server/media_lineage/integration.py) | 压缩适配 | 将压缩 mission/直接压缩接入血缘契约，核对任务代次与分组绑定；提供 `compression_gpu()`，仅在实际 GPU 编码期间申请一个共享压缩槽位。依赖 `EnvConfig`，部分路径使用应用 DB。 |
| [resources.py](../../automation-server/media_lineage/resources.py) | GPU 资源管理 | 解析物理 GPU 身份，查询驱动空闲/进程显存和利用率，原子准入、未兑现预留、安全余量、心跳、释放、OOM 预算学习、等待公平性和文件锁；服务于提取、MIL 推理/训练与压缩。 |
| [process_tree.py](../../automation-server/media_lineage/process_tree.py) | 子进程基础设施 | 管理自身创建的进程树：Windows Job Object、POSIX 进程组、进程身份、终止与回收。分类 worker 与 FFmpeg 编码均使用。 |
| [tests/__init__.py](../../automation-server/media_lineage/tests/__init__.py) | 测试包 | 声明共享契约测试包，不注册生产流程。 |
| [tests/test_events.py](../../automation-server/media_lineage/tests/test_events.py) | 血缘测试 | 覆盖事件重放、冲突、阶段顺序、身份一致性、不可更新/删除及 PostgreSQL 事件分配串行化。 |
| [tests/test_operations.py](../../automation-server/media_lineage/tests/test_operations.py) | 流转测试 | 覆盖重编码身份映射、发布前后证据、源替换/缺失、独立执行身份，以及压缩不会为未知来源凭空赋喜欢标签。 |
| [tests/test_compression_gpu.py](../../automation-server/media_lineage/tests/test_compression_gpu.py) | 共享资源测试 | 覆盖压缩与提取共享显存、压缩最多一个、训练独占、等待/超时/心跳、编码进程注册、CPU 复制不申请 GPU，以及仪表盘准入诊断。 |

需要保留的命名区别：数据库租约仍允许历史 `exclusive_compression` 模式；当前压缩新路径申请的是 `extract_shared`，以 `workload_type="compression"` 区分，并限制一个编码任务。历史枚举名称不能用来判断当前压缩一定独占。

`media_lineage` 没有自己的 API、页面或独立调度器；其测试目前复用 `video_filter/tests/support.py` 的隔离数据库设施。日志名 `media_lineage.*` 由框架根日志处理，当前没有单独配置一个 `media_lineage/logs/` 路由。

## 4. video_filter：文件逐项说明

目录：[automation-server/video_filter/](../../automation-server/video_filter/)。下表分类是职责索引，便于审查，不表示已经调整物理目录。

### 4.1 注册、配置与分组隔离

| 文件 | 具体作用与边界 |
| --- | --- |
| [__init__.py](../../automation-server/video_filter/__init__.py) | `register_video_filter()` 注册 ORM、API、工作台和启用后的定时入口；重复注册有保护，包导入自身不启动服务。 |
| [group_config.py](../../automation-server/video_filter/group_config.py) | 纯配置校验：组名、五个目录角色、训练选项、路径锚定、重叠/链接检查和采样范围检查；不访问数据库、不枚举媒体。 |
| [groups.example.json](../../automation-server/video_filter/groups.example.json) | 分组结构示例，供用户建立本地实际配置；不是数据库状态或自动扫描结果。 |
| [configuration.py](../../automation-server/video_filter/configuration.py) | 将组名及目录角色映射保存为不可变 `ConfigRevision`，通过摘要去重；先记录 pending，激活由对账流程完成。分组运行参数和模型超参不混入目录版本摘要。 |
| [scope.py](../../automation-server/video_filter/scope.py) | 建立组/重置代次上下文、初始化组状态、限制 ORM 查询/写入的组范围；将运行时状态目录定位为 `state/groups/<group-id>/<epoch>/`。 |
| [training_config.py](../../automation-server/video_filter/training_config.py) | 按模型类型计算有效训练超参与配置摘要；定义自动重训练新增样本阈值 100，供入队去重和训练快照核验使用。 |

### 4.2 文件追踪、用户反馈与安全搬运

| 文件 | 具体作用与边界 |
| --- | --- |
| [identity.py](../../automation-server/video_filter/identity.py) | 筛选专用身份接口：支持的视频扩展名、源缺失异常、读取前后快照核验；复用公共文件身份能力，与偏好标签无关。 |
| [notifications.py](../../automation-server/video_filter/notifications.py) | Windows 非递归目录通知、变化/溢出提示、监视线程回收；通知仅触发对账，不直接写喜欢/不喜欢标签。 |
| [tracking.py](../../automation-server/video_filter/tracking.py) | 枚举组内允许目录、登记稳定文件、消费压缩/搬运血缘、识别移动和重编码、激活配置版本、确认缺失并生成反馈；保护扫描不完整、路径变更及尚未完成的流转。 |
| [feedback.py](../../automation-server/video_filter/feedback.py) | 通过标签版本和幂等事件键提交用户反馈；先写本地耐久 journal，再入库并回放恢复。处理喜欢/不喜欢、模型失效及实际结果关联；预测标签不作为训练反馈。 |
| [transfer.py](../../automation-server/video_filter/transfer.py) | 分类搬运前检查特征、预测、配置、反馈及源快照；不覆盖目标，使用保留的硬链接见证恢复搬运，记录发布/清理状态及血缘。 |

### 4.3 调度、任务与执行监督

| 文件 | 具体作用与边界 |
| --- | --- |
| [tasks.py](../../automation-server/video_filter/tasks.py) | 持久任务去重、输入快照、原子认领和有限失败重试；定义事务错误分类。创建任务不直接启动 worker。 |
| [runtime.py](../../automation-server/video_filter/runtime.py) | 业务编排：发现提取/训练/预测/分类候选、检查前置条件和新增样本门槛、入队、按任务类型执行；保留旧 `process_round()` 路径，常规分组入口注册后交由 supervisor。 |
| [supervisor.py](../../automation-server/video_filter/supervisor.py) | 执行监督：短控制轮次、跨组公平补位、独立线程/DB session、目录扫描、任务恢复/取消、worker GPU 阶段租约、心跳、完成后唤醒、OOM 并发调整与关闭回收。 |
| [worker_client.py](../../automation-server/video_filter/worker_client.py) | 父进程统一任务传输接口：准备请求及临时 NPZ、选择一次性/常驻子进程、转发日志与 GPU 请求、超时取消、校验结果；提供提取、MIL 训练与 MIL 推理三个调用入口。 |
| [persistent_client.py](../../automation-server/video_filter/persistent_client.py) | 父进程常驻 worker 池：按用途/设备/CPU 线程/缓存预算等分池，限制容量，租用和归还进程，串行执行单 worker 任务，处理闲置超时、任务数退休、异常销毁及池关闭。 |
| [persistent_worker.py](../../automation-server/video_filter/persistent_worker.py) | 常驻子进程 stdin 命令循环，执行提取或 MIL 推理，维持 CPU 模型缓存，发送逐任务完成消息；异常记录堆栈后退出，不接触 Flask/数据库/调度器。 |
| [worker.py](../../automation-server/video_filter/worker.py) | 提取任务实现 `execute()` 及一次性 CLI：加载请求/特征契约、解码和提取、校验源、生成数组与元信息。常驻进程也调用同一个实现。 |
| [training_worker.py](../../automation-server/video_filter/training_worker.py) | 一次性 MIL 训练子进程：读取窗口 bag 数据、CPU 准备、申请训练 GPU 阶段、拟合和验证、输出数值参数/测量结果；训练不走常驻池。 |
| [inference_worker.py](../../automation-server/video_filter/inference_worker.py) | MIL 推理实现 `execute()` 及一次性 CLI：读取数值模型和窗口 bag，准备 PyTorch 模型，GPU 预测并输出概率；可由常驻进程复用。 |
| [worker_gate.py](../../automation-server/video_filter/worker_gate.py) | 子进程启动闸门：等父进程已纳管进程树再开始模型/解码工作，避免未受控子进程抢先运行。 |
| [gpu_phase.py](../../automation-server/video_filter/gpu_phase.py) | worker 侧 GPU 阶段协议：通过 stdout JSON/stdin 回复申请、释放或报告失败；数据库 GPU 租约由父进程持有。 |
| [process_tree.py](../../automation-server/video_filter/process_tree.py) | 兼容导出公共 `media_lineage.process_tree.ProcessTree`；这里没有另一套进程树实现。 |
| [model_cache.py](../../automation-server/video_filter/model_cache.py) | 常驻 worker 内有上限的 CPU 模型 LRU：计算权重/支撑文件身份缓存键、按大小淘汰、超预算对象不入缓存、跨视频重置 OOM 缩批状态；MIL 模型按组/代次/模型校验和区分。 |

### 4.4 解码、特征契约与模型适配

| 文件 | 具体作用与边界 |
| --- | --- |
| [media.py](../../automation-server/video_filter/media.py) | ffprobe 媒体探测、FFmpeg/NVDEC 视频抽帧、CPU 音频解码、连续时间戳对齐的音频窗口、进程超时清理、保留颜色字段/空输出恢复；不持久保存截图、音频片段或转录。 |
| [extraction.py](../../automation-server/video_filter/extraction.py) | 单视频特征提取总入口：源身份验证、对齐窗口、四种模态与有效性掩码、结果和耗时测量；正常真实 decoder 使用批量路径，保留兼容/测试用逐窗口路径。 |
| [batched_extraction.py](../../automation-server/video_filter/batched_extraction.py) | 顺序运行四模态，以有界窗口批次组织输入；同一音频窗口复用给 BEATs/eGeMAPS，CPU 音频与声学分析放在 GPU 租约前，NVDEC/神经网络在租约内；成功阶段结束将模型迁回 CPU。 |
| [features/__init__.py](../../automation-server/video_filter/features/__init__.py) | 适配器包入口说明；模型由 worker 显式加载，包导入时不加载权重。 |
| [features/contract.py](../../automation-server/video_filter/features/contract.py) | 特征版本契约：四模态维度、窗口策略、预处理、聚合及规范 JSON/摘要；贯穿提取、存储、训练和预测兼容性判断。 |
| [features/manifest.py](../../automation-server/video_filter/features/manifest.py) | 读取本地模型清单，核验权重/BEATs 源码/支撑配置的身份与校验和；通过文件身份及周期复验缓存结果，无自动下载兜底。 |
| [features/batching.py](../../automation-server/video_filter/features/batching.py) | 通用有序批推理、输出校验、CUDA OOM 后缩小批次重试、模型 CPU/GPU 迁移及缓存释放；由各冻结神经模型适配器复用。 |
| [features/dino.py](../../automation-server/video_filter/features/dino.py) | 冻结 DINOv2 ViT-S/14，本地 checkpoint 加载、画面预处理和帧批推理，输出 384 维视觉特征。 |
| [features/videomae.py](../../automation-server/video_filter/features/videomae.py) | 冻结 VideoMAE Small，读取本地模型与预处理配置，执行视频片段批推理，输出 384 维时序编码特征。 |
| [features/beats.py](../../automation-server/video_filter/features/beats.py) | 加载固定版本的 BEATs 源码与权重，执行音频分块/批推理，输出 768 维深度声音特征。 |
| [features/acoustic.py](../../automation-server/video_filter/features/acoustic.py) | 校验 openSMILE 版本及配置树，提取 eGeMAPSv02 Functionals 的 88 维 CPU 声学统计，标记无声/无浊音/无效项；不是 ASR。 |
| [feature_store.py](../../automation-server/video_filter/feature_store.py) | 校验窗口数组、有效性及聚合摘要，序列化 NPZ 和 manifest，计算校验和，将它们与 ready 状态一起持久化到数据库；读取已提交的完整 bundle，拒绝损坏/不兼容摘要。 |

DINO/VideoMAE/BEATs 是冻结的通用特征提取器，不会随着用户标签训练。个性化训练发生在下一节的 LR/MIL。eGeMAPS 是 CPU 上的声学统计提取器，不是另一个待训练神经模型。

### 4.5 个性化训练、预测与实际准确率

| 文件 | 具体作用与边界 |
| --- | --- |
| [model_registry.py](../../automation-server/video_filter/model_registry.py) | 声明 `logistic_regression`/`mil` 两种分类器及独立 active slot，查询当前兼容的激活模型；不实现拟合过程。 |
| [learning.py](../../automation-server/video_filter/learning.py) | 从数据库确认标签构建训练集，LR 使用聚合向量、MIL 使用窗口 bag；独立训练/验证/选择阈值、核对标签快照、保存和激活模型。LR 拟合在 CPU，MIL 训练/推理委托子进程；模型以数值参数存入数据库。 |
| [mil.py](../../automation-server/video_filter/mil.py) | MIL 算法：窗口 bag 构造、标准化、门控注意力网络、PyTorch 拟合/推理、参数数值序列化和还原；提供 NumPy 概率参考实现用于一致性校验。 |
| [prediction.py](../../automation-server/video_filter/prediction.py) | 同一源/摘要/标签/模型版本下的预测复用，执行各激活分类器并独立存储概率、阈值、标签和 selected 状态；提交前重核源及模型，记录预测批次和实际评价资格。 |
| [evaluation.py](../../automation-server/video_filter/evaluation.py) | 将预测之后的用户反馈关联到 `PredictionOutcome`，按最新用户判断和 asset 去重；统计每类模型/每版本及同批配对的真实准确率、混淆矩阵、精确率和置信区间，排除训练/验证数据泄漏。 |

### 4.6 数据库、控制接口、展示与可观测性

| 文件 | 具体作用与边界 |
| --- | --- |
| [models/__init__.py](../../automation-server/video_filter/models/__init__.py) | 显式导出筛选 ORM 模型，供注册、业务代码和 Alembic 加载。 |
| [models/records.py](../../automation-server/video_filter/models/records.py) | 集中定义分组、代次、视频身份、扫描位置、特征、反馈、任务、模型、预测/实际结果和搬运表；定义约束、分组索引及 ORM 隔离保护。 |
| [apis/__init__.py](../../automation-server/video_filter/apis/__init__.py) | 控制 API 包入口说明，路由由主注册函数显式加载。 |
| [apis/control.py](../../automation-server/video_filter/apis/control.py) | 本地 HTTP 输入校验及事务边界，提供组列表、状态、任务入队/重试/查询、资产、反馈及实际指标；进入/退出分组作用域，不在 HTTP 请求中直接进行特征提取。 |
| [reporting.py](../../automation-server/video_filter/reporting.py) | 为 JSON API 和 Jinja 页生成只读数据库/调度/GPU 状态快照、分组统计与任务明细；与模板共享数据口径。 |
| [dashboard.py](../../automation-server/video_filter/dashboard.py) | 注册筛选工作台页和局部刷新端点，选择分组、生成中文显示名称、处理禁用/配置/表结构错误；依赖公共 dashboard 外壳。 |
| [templates/video_filter/dashboard.html](../../automation-server/video_filter/templates/video_filter/dashboard.html) | 继承公共外壳，提供筛选页面容器与刷新入口。 |
| [templates/video_filter/_snapshot.html](../../automation-server/video_filter/templates/video_filter/_snapshot.html) | 渲染分组进度、摘要/样本统计、任务阶段、GPU 准入、模型和实际准确率的局部 HTML；刷新端点返回此模板。 |
| [observability.py](../../automation-server/video_filter/observability.py) | 状态日志限频、完整异常链、路径脱敏注册、worker stdout JSON 日志转发及耗时阶段心跳；常驻 worker 切换任务时更换日志处理器。 |
| [progress.py](../../automation-server/video_filter/progress.py) | 按字段白名单读写任务进度 JSON，原子替换/冲突恢复和日志 handler 生命周期；任务持久状态仍在数据库，进度文件供页面实时展示。 |

### 4.7 环境准备和手动诊断工具

| 文件 | 具体作用与边界 |
| --- | --- |
| [requirements-local.txt](../../automation-server/video_filter/requirements-local.txt) | 本地特征与训练依赖版本记录，说明 CUDA torch 系列 wheel；不是服务启动脚本，也不包含全部 Flask/DB 基础依赖。 |
| [provision.py](../../automation-server/video_filter/provision.py) | 显式手动准备本地权重、VideoMAE 配置、BEATs 源码和声学签名，校验下载文件并生成 manifest；支持代理，会联网下载。常规推理不调用它。 |
| [diagnostics.py](../../automation-server/video_filter/diagnostics.py) | 手动检查依赖、CUDA、FFmpeg/NVDEC 和指定样本技术信息；样本检查限制在明确给定组范围，不导入生产 app，不写生产数据库。 |
| [smoke.py](../../automation-server/video_filter/smoke.py) | 手动对明确指定且组内的样本做冻结适配器试运行，输出维度与测量数据；不写生产数据库。 |

## 5. 测试文件逐项说明

这些文件是验证代码，不由服务调度器调用。普通离线测试使用临时/隔离资源；专门的 GPU、FFmpeg 验证脚本有实际硬件或工具依赖，不能当作纯离线检查。

| 文件（均在 video_filter/tests/） | 具体验证内容 |
| --- | --- |
| [__init__.py](../../automation-server/video_filter/tests/__init__.py) | 标明测试使用注入的 SQLite app，避免生产 app/.env 注册链。 |
| [support.py](../../automation-server/video_filter/tests/support.py) | 构造隔离 Flask/SQLAlchemy 扩展、临时状态目录、合成特征与身份 fixture、测试基类；同时被 media_lineage 测试复用。 |
| [test_config.py](../../automation-server/video_filter/tests/test_config.py) | `EnvConfig` 筛选配置、启停和路径/数值输入校验。 |
| [test_configuration.py](../../automation-server/video_filter/tests/test_configuration.py) | 配置快照规范化、摘要去重、目录版本记录。 |
| [test_control.py](../../automation-server/video_filter/tests/test_control.py) | 控制接口的状态/查询/输入错误和反馈行为。 |
| [test_feature_contract.py](../../automation-server/video_filter/tests/test_feature_contract.py) | 特征签名、窗口对齐、序列化及版本摘要一致性。 |
| [test_manifest.py](../../automation-server/video_filter/tests/test_manifest.py) | 本地权重/代码/支撑配置清单校验、损坏拒绝与缓存失效/周期复验。 |
| [test_feature_store.py](../../automation-server/video_filter/tests/test_feature_store.py) | NPZ/manifest 校验、数据库原子存储、ready 门槛、聚合/掩码及损坏 payload 拒绝。 |
| [test_tasks.py](../../automation-server/video_filter/tests/test_tasks.py) | 入队去重、标签版本变化、认领/输入契约和有限重试。 |
| [test_workflow.py](../../automation-server/video_filter/tests/test_workflow.py) | 扫描稳定期、移动/重命名/删除反馈、反馈重放、安全搬运恢复、压缩血缘及无关压缩源不会被扫描。 |
| [test_runtime.py](../../automation-server/video_filter/tests/test_runtime.py) | 自动发现/有界候选队列、100 个新增样本重训练、手动训练、入队接口、注册幂等与禁用路径。 |
| [test_groups.py](../../automation-server/video_filter/tests/test_groups.py) | 多组查询/标签/特征隔离、目录版本、删除样本保留、五角色流转、部分扫描和压缩快速流转保护、组配置校验及共享资源约束。 |
| [test_group_runtime.py](../../automation-server/video_filter/tests/test_group_runtime.py) | 多视频补位并发、控制轮次唤醒、公平调度、OOM 降级、取消/关闭回收、死锁重试、待反馈挡板及 GPU 阶段租约。 |
| [test_dual_classifiers.py](../../automation-server/video_filter/tests/test_dual_classifiers.py) | LR/MIL 独立训练/激活/预测、模型参数、真实评价资格、用户判断关联与配对统计。 |
| [test_gpu_inference.py](../../automation-server/video_filter/tests/test_gpu_inference.py) | NVDEC 命令和采样契约、音频无需视频解码器、MIL GPU 数值/传输接口；使用隔离或合成数据，实际硬件另测。 |
| [test_batch_phases.py](../../automation-server/video_filter/tests/test_batch_phases.py) | 四模态批推理顺序/对齐、批大小、OOM 缩批、CPU/GPU 迁移及父子 GPU 协议边界。 |
| [test_media_recovery.py](../../automation-server/video_filter/tests/test_media_recovery.py) | 连续 PCM 解码对齐、颜色字段恢复、空视频采样恢复、音频缺失窗口和有界读缓冲；可选真实 FFmpeg 合成音频对照。 |
| [test_performance.py](../../automation-server/video_filter/tests/test_performance.py) | 性能配置、显存增量准入/预留/预算学习与音频复用约束；验证策略正确性，不代表真实视频吞吐基准。 |
| [test_persistent_workers.py](../../automation-server/video_filter/tests/test_persistent_workers.py) | CPU 模型缓存 LRU/失效、进程和模型复用、独立任务输出、MIL 版本切换、池容量、闲置/任务数退休、取消/超时/坏结果回收及日志隔离。 |
| [test_logging.py](../../automation-server/video_filter/tests/test_logging.py) | 独立 error/performance 日志、脱敏、完整远程/本地堆栈、训练进度、worker 日志实时转发和超时失败。 |
| [test_dashboard.py](../../automation-server/video_filter/tests/test_dashboard.py) | 公共外壳和页面注册、只读展示/转义、故障显示、真实任务阶段和进度文件原子替换/损坏处理。 |
| [test_migration.py](../../automation-server/video_filter/tests/test_migration.py) | Alembic 表结构、特征/模型 DB 化、双模型/分组及 GPU 预算变更与保护条件。 |
| [audio_validation.py](../../automation-server/video_filter/tests/audio_validation.py) | 手动调用配置的 FFmpeg，用合成 PCM 核对连续音频解码；不扫描实际媒体目录。 |
| [gpu_validation.py](../../automation-server/video_filter/tests/gpu_validation.py) | 手动组内样本 GPU 基准，在隔离 DB 中提取并校验摘要；要求显式声明服务已停止，可测试指定并发，避免与生产资源混用。 |
| [compression_regression.py](../../automation-server/video_filter/tests/compression_regression.py) | 注入隔离 app/配置后运行已有 video_compression 测试；这是测试依赖压缩项目，不是筛选运行时调用压缩业务实现。 |

## 6. 仓库其他位置中的相关文件

### 6.1 服务外壳、配置与前端公共资源

| 文件 | 与两个模块的关系 |
| --- | --- |
| [automation-server/app.py](../../automation-server/app.py) | 创建共享 Flask app、db、scheduler，安装日志/异常处理，读取数据库配置；业务 ORM 与 API 依赖此扩展。 |
| [automation-server/main.py](../../automation-server/main.py) | 导入业务项目并显式注册 video_filter，实际启动 scheduler 与 Flask 服务。 |
| [automation-server/env.py](../../automation-server/env.py) | 唯一运行时配置入口；读取分组 JSON、公共模型/状态/并发/GPU 参数，负责相对路径锚定。本文不展开其配置指向的目录。 |
| [automation-server/.env.example](../../automation-server/.env.example) | 可公开的环境变量结构和占位值示例。 |
| `automation-server/.env`（Git 忽略） | 本地真实标量配置，由 EnvConfig 读取；不是源码或摘要数据，本次不复述配置值。 |
| [.gitignore](../../.gitignore) | 定义 state、weights、logs、压缩 cache、本地 .env、private 和 MP4 等忽略范围；被忽略不代表程序不用。 |
| [automation-server/logging_config.py](../../automation-server/logging_config.py) | 统一脱敏、运行/error 文件路由与轮转、异常链边界、JSONL 性能记录；worker 通过协议把日志转交父进程落盘。 |
| [automation-server/dashboard/__init__.py](../../automation-server/dashboard/__init__.py) | 公共工作台 Blueprint、首页、服务导航注册，未来其他项目可复用。 |
| [automation-server/dashboard/templates/dashboard/base.html](../../automation-server/dashboard/templates/dashboard/base.html) | 公共 Jinja 外壳、导航、CSS/JS 引入。 |
| [automation-server/dashboard/templates/dashboard/index.html](../../automation-server/dashboard/templates/dashboard/index.html) | 展示已注册服务的工作台首页。 |
| [automation-server/dashboard/static/dashboard.js](../../automation-server/dashboard/static/dashboard.js) | 页面局部刷新、状态显示和刷新错误处理；加载 video_filter 快照片段。 |
| [automation-server/dashboard/static/dashboard.css](../../automation-server/dashboard/static/dashboard.css) | 公共工作台样式、布局及状态展示。 |
| [automation-server/dashboard/static/favicon.svg](../../automation-server/dashboard/static/favicon.svg) | 工作台图标。 |
| [automation-server/tests/__init__.py](../../automation-server/tests/__init__.py) | 框架测试包入口。 |
| [automation-server/tests/test_logging_config.py](../../automation-server/tests/test_logging_config.py) | 验证共享日志配置、脱敏和文件路由等基础行为。 |
| [automation-server/tests/test_exception_boundaries.py](../../automation-server/tests/test_exception_boundaries.py) | 验证 Flask/线程/未捕获异常等公共日志边界。 |
| [env-setup.sh](../../env-setup.sh) | 仓库基础 autoflow 环境准备命令记录；不能替代 video_filter 的 CUDA/特征依赖准备，服务不会自动执行它。 |
| [README.md](../../README.md) | 全项目入口与使用说明，包含筛选、日志、GPU 阶段和常驻 worker 配置说明。 |

当前工作区没有独立的 `automation-server/init_db.py`，也没有发现独立 `init_db` 实现；数据库结构入口是下面的 Alembic 迁移链，不能把规则文件中的参考文件名当成现存代码。

### 6.2 数据库迁移

| 文件 | 具体作用 |
| --- | --- |
| [automation-server/alembic.ini](../../automation-server/alembic.ini) | Alembic 工程配置；真实数据库连接由 EnvConfig 提供。 |
| [automation-server/migrations/env.py](../../automation-server/migrations/env.py) | 加载共享 ORM 元数据，包括 video_filter/models 和 media_lineage/models，执行迁移环境配置。 |
| [f3a81c9d6024_add_video_filter_foundation.py](../../automation-server/migrations/versions/f3a81c9d6024_add_video_filter_foundation.py) | 最初筛选基础表与公共媒体血缘事件表。 |
| [b6c20d8e7419_store_video_filter_features_in_database.py](../../automation-server/migrations/versions/b6c20d8e7419_store_video_filter_features_in_database.py) | 摘要数值/manifest 入库和 ready 约束；保留历史文件引用，要求旧摘要显式转换。 |
| [d9e41b7a2036_add_video_filter_workflow.py](../../automation-server/migrations/versions/d9e41b7a2036_add_video_filter_workflow.py) | 持久扫描 observation、活跃配置、分类器参数入库和血缘 evidence。 |
| [a7d52e9c1048_add_dual_classifiers_and_outcomes.py](../../automation-server/migrations/versions/a7d52e9c1048_add_dual_classifiers_and_outcomes.py) | 独立 LR/MIL 模型槽位、预测批次/阈值/评价资格及预测实际结果表。 |
| [c4f18a2d9076_group_video_filter_and_resources.py](../../automation-server/migrations/versions/c4f18a2d9076_group_video_filter_and_resources.py) | 在显式清理旧筛选数据后重建分组约束，增加组/代次、公共 workflow binding 和资源租约表；保护共享血缘和压缩数据。 |
| [d2e90b1746a3_add_gpu_memory_budgets.py](../../automation-server/migrations/versions/d2e90b1746a3_add_gpu_memory_budgets.py) | 租约增加显存预算和观测 JSON，用于增量准入与峰值/OOM 记录。 |

这些是结构演进记录，运行时不应把旧迁移当成最新模型定义；当前表结构定义看 `models/records.py` 和 `media_lineage/models.py`。本文仅审查文件，未执行迁移。

### 6.3 video_compression 中的实际协作点

| 文件/目录 | 与本次范围的关系 |
| --- | --- |
| [video_compression/__init__.py](../../automation-server/video_compression/__init__.py) | 压缩项目注册入口，由 main.py 独立导入。 |
| [video_compression/schedules/compress_videos.py](../../automation-server/video_compression/schedules/compress_videos.py) | 压缩任务发现/领取/恢复/发布清理，读取公共分组绑定，调用 integration 记录源到输出的血缘。 |
| [video_compression/service.py](../../automation-server/video_compression/service.py) | 视频探测、压缩判定、原字节安全复制或重编码、验证和暂存发布；直接调用路径也接公共血缘。 |
| [video_compression/transcoder.py](../../automation-server/video_compression/transcoder.py) | 构建/执行 FFmpeg GPU 编码命令，实际 GPU 阶段申请共享压缩预算，纳管进程树并续租；后续 CPU 验证/复制不占用 GPU 租约。 |
| [video_compression/tests/test_transcoder.py](../../automation-server/video_compression/tests/test_transcoder.py) | FFmpeg 执行、进度、失败/终止行为的相邻回归测试；与公共压缩 GPU 测试共同验证资源生命周期。 |
| `automation-server/video_compression/cache/`（Git 忽略） | 压缩服务默认暂存编码产物及恢复见证，包含 `.mission-<id>.transcoding.mp4` 或随机名临时 MP4；不作为筛选特征存储。 |
| `automation-server/video_compression/logs/`（Git 忽略） | 压缩 runtime/error 日志；与筛选日志一起用于观察共享 GPU 等待和流转故障。 |

这里列的是协作接口及其直接实现，不展开整个压缩项目。video_filter 通过公共 media_lineage 消费压缩结果和协调资源，常规分类代码不直接导入压缩业务内部实现；测试桥接脚本另见第 5 节。

### 6.4 本项目已有说明与手动 SQL

| 文件（均在 plan/video_filter/） | 当前内容用途 |
| --- | --- |
| [research.md](research.md) | 执行效率/并发不足排查、原准入问题和增量显存方案；属于当时调研记录，不能代替最新源码。 |
| [codingplan.md](codingplan.md) | 吞吐与并发调度优化 P0—P8 编码计划。 |
| [implementation.md](implementation.md) | 初期完整流程、DB 摘要/参数、目录和日志启用说明；含早期结构语境。 |
| [grouped-implementation.md](grouped-implementation.md) | 多组目录、隔离训练/预测、反馈/路径恢复、并发和后续 GPU 调整记录。 |
| [dual-classifiers.md](dual-classifiers.md) | LR/MIL 并行预测、实际准确率统计、模型选择与验证边界。 |
| [dashboard.md](dashboard.md) | 本地 Jinja 工作台、公共外壳及未来服务页面注册说明。 |
| [performance-implementation.md](performance-implementation.md) | 性能改造结果、预算配置、部署及只读诊断 SQL。 |
| [clear_legacy_data.sql](clear_legacy_data.sql) | 用户手动执行的旧筛选数据清理 SQL，带任务/数据保护条件；不是服务启动时自动执行的脚本。 |
| [file-responsibilities.md](file-responsibilities.md) | 本次文件职责索引，辅助重新归类。 |

## 7. Git 忽略的文件、运行时目录和外部依赖

### 7.1 预训练模型：weights/

本次只核对模型目录结构和源码用途，未读取权重二进制。下列名称在当前工作区存在，并由 `provision.py` 准备、manifest/适配器读取。整个 `automation-server/video_filter/weights/` 被 Git 忽略。

| 文件（相对 weights/） | 具体作用 | 性质 |
| --- | --- | --- |
| `manifest.json` | 声明四模态权重相对路径、架构、输入/预处理、支撑文件哈希、窗口与聚合策略；计算特征签名。 | 模型契约，运行必需。 |
| `dinov2_vits14_pretrain.pth` | DINOv2 ViT-S/14 冻结 checkpoint。 | 通用画面权重。 |
| `videomae/pytorch_model.bin` | VideoMAE Small 冻结 checkpoint。 | 通用时序权重。 |
| `videomae/config.json` | VideoMAE 架构参数和模型加载配置。 | 权重支撑配置。 |
| `videomae/preprocessor_config.json` | VideoMAE 视频预处理参数；契约与缓存失效检查也使用。 | 权重支撑配置。 |
| `BEATs_iter3.pt` | BEATs Iter3 冻结 checkpoint。 | 通用音频权重。 |
| `beats-source/BEATs.py` | 官方 BEATs 配置、声学输入预处理与特征提取主体。 | 实际执行的第三方 Python 代码。 |
| `beats-source/backbone.py` | BEATs Transformer encoder、层和注意力实现。 | 实际执行的第三方 Python 代码。 |
| `beats-source/modules.py` | BEATs 使用的激活、门控、padding、梯度/噪声辅助组件。 | 实际执行的第三方 Python 代码。 |
| `egemaps-config-signature.json` | 记录 openSMILE 版本、eGeMAPSv02 维度和配置树摘要；不是可训练的神经权重。 | 声学配置契约。 |
| `beats-source/__pycache__/*.pyc` | 导入第三方 Python 模块时生成的字节码。 | 可再生缓存。 |

特别需要分类的是 `weights/beats-source/*.py`：这些虽然放在 weights 下且不在 Git 中，仍是运行时执行的代码，不只是模型数据。加载方 `features/beats.py` 核验 manifest 中固定的源码哈希。

准备下载时还可能短暂产生 `*.download`，完成或失败后由 provision 清理。LR/MIL 的用户训练参数不存放在 weights 下。

### 7.2 状态与临时传输：state/

整个默认 `automation-server/video_filter/state/` 被 Git 忽略。当前顶层有 `control.lock`、`gpu.lock`、`feedback/`、`groups/`；顶层遗留目录/锁的存在不能说明当前分组一定还在使用它们。

| 路径或模式（相对 state/） | 写入/使用方 | 内容与生命周期 |
| --- | --- | --- |
| `control.lock` | supervisor / 公共 `gpu_lock()` | 全局控制轮次互斥；锁文件存在不等于当前仍被占用。 |
| `gpu.lock` | 公共资源锁接口及历史/兼容执行路径 | GPU 相关文件互斥载体；当前分阶段 GPU 准入以数据库租约为准。 |
| `groups/<group-id>/<epoch>/` | scope | 隔离同组当前代次的运行时文件；目录按组 ID/代次生成，不以媒体目录路径作为数据身份。 |
| `groups/<group-id>/<epoch>/control.lock` | supervisor | 单组扫描/控制互斥。 |
| `groups/<group-id>/<epoch>/feedback/*.json` | FeedbackJournal | 尚待提交/重放的用户反馈耐久记录，含 asset、label revision、事件键、证据等；成功提交后清理。**不是可以随意丢弃的缓存。** |
| `groups/<group-id>/<epoch>/progress/<task-id>.json` | ProgressHandler / reporting | 阶段、模态、计数、耗时、训练 epoch 等实时元数据；不存媒体/训练特征，终态以 DB 为准，不能凭文件存在判任务运行。 |
| `groups/<group-id>/<epoch>/extraction-<random>/` | worker_client / worker | `request.json`、启动 `gate`、`output/arrays.npz` 和 `output/metadata.json`；传输提取结果，父进程校验后入库。正常退出后临时目录清理。 |
| `groups/<group-id>/<epoch>/training-<random>/` | worker_client / training_worker | `request.json`、`gate`、`dataset.npz`、`output/result.json`；向训练子进程传递窗口 bags/划分/标签并收回参数和测量。 |
| `groups/<group-id>/<epoch>/prediction-<random>/` | worker_client / inference_worker | `request.json`、`gate`、`model.npz`、`bag.npz`、`output/result.json`；MIL 推理的本地中间传输。 |
| 顶层 `feedback/` 及旧代次目录 | 历史/兼容路径可能留下 | 本次未读取内容或判定是否可删除；迁移分类时需区分未提交反馈、旧代次与普通临时产物。 |

临时目录只是父子进程传输协议，**不是训练数据的永久保存位置**。异常退出可能留下残余文件，不能仅凭目录名称断言已经安全回收。

进程 stderr 使用 `tempfile.TemporaryFile()`，由 Python/操作系统临时文件机制管理，不是额外固定的项目日志目录。CPU 模型 LRU、模型清单校验缓存、PCM 窗口缓冲和常驻进程池是内存对象，没有一套对应的永久缓存文件。

### 7.3 日志和生成文件

| 路径/模式 | 具体作用 |
| --- | --- |
| `automation-server/video_filter/logs/runtime.log` | 发现/排队/执行、特征入库、训练/预测、搬运与反馈等运行信息。 |
| `automation-server/video_filter/logs/error.log` | error 级别异常和完整堆栈，包括子进程失败经父进程转发后的异常链。 |
| `automation-server/video_filter/logs/performance.log` | 单独的 JSONL 性能事件：worker 启动/复用、缓存命中/加载、批大小、CPU/GPU 阶段耗时、显存准入/峰值/OOM、资源等待等。 |
| 同目录 `*.log.1`…`*.log.10` | 默认单日志达到 10 MiB 时轮转的备份，实际数量随运行生成。 |
| `automation-server/logs/runtime.log`、`error.log` 及轮转文件 | 框架与公共模块日志，包括 media_lineage logger 的根日志路由。 |
| 各 Python 包的 `__pycache__/*.pyc` | Python 生成的字节码，被 `*.pyc` 忽略；不属于源码文件职责清单。 |
| `plan/*.mp4` | 当前两个用户提供的喜欢/不喜欢手动测试样例；被全局 `*.mp4` 忽略，不是服务自动发现的数据来源。本文不复述媒体文件名。 |

日志可能含业务身份及故障证据，本文没有展开内容。`.playwright-mcp/` 为浏览器工具产物忽略项，当前两个业务模块没有把它作为运行数据依赖，因此不纳入业务目录分类。

### 7.4 数据库中实际保存的内容

数据库是 EnvConfig 指定的共享数据库，本次不读取生产连接或枚举数据库所在目录。下面解释数据归属，以免把它们误归为 state/weights 中的文件。

| 模型/表 | 保存内容 |
| --- | --- |
| `DatasetGroup` / `video_filter_dataset_group` | 稳定组身份、名称、启用状态、代次和血缘起点。 |
| `RuntimeState` / `video_filter_runtime_state` | 当前运行数据重置代次。 |
| `Asset` / `video_filter_asset` | 视频逻辑身份、最终用户标签及标签版本。 |
| `Variant` / `video_filter_variant` | 同一逻辑视频的具体字节版本、hash 和大小，连接重编码前后。 |
| `ConfigRevision` / `video_filter_config_revision` | 目录角色映射快照、摘要、pending/active 等状态。 |
| `Observation` / `video_filter_observation` | 扫描稳定期的路径身份、大小/修改时间、最近观测时间。 |
| `ScanRun` / `video_filter_scan_run` | 一次扫描的范围、完整性/错误和对账状态。 |
| `Location` / `video_filter_location` | 视频版本所在路径/角色、存在/缺失状态、身份及反馈追踪。 |
| `FeatureBundle` / `video_filter_feature_bundle` | `arrays_blob` 中的 NPZ：四模态窗口向量、有效性/聚合摘要；manifest、校验和、feature signature、ready 状态。 |
| `FeedbackEvent` / `video_filter_feedback_event` | 用户喜欢/不喜欢历史、版本、幂等键和证据。 |
| `Task` / `video_filter_task` | 持久队列、任务快照、认领/心跳、重试、终态和失败诊断。 |
| `ModelRun` / `video_filter_model_run` | 分组独立 LR/MIL 参数 `model_blob`、超参、训练样本/标签快照、阈值、验证指标和激活版本。 |
| `Prediction` / `video_filter_prediction` | 两种模型各自的概率、阈值、预测标签、选用标志、批次、版本及实际评价资格。 |
| `PredictionOutcome` / `video_filter_prediction_outcome` | 历史预测与之后用户实际反馈的关联、实际标签与是否正确。 |
| `TransferOperation` / `video_filter_transfer_operation` | 程序分类搬运的源/目标、见证及恢复阶段。 |
| `LineageEvent` / `media_lineage_event` | 公共源/目标 hash、路径、操作代次、发布/清理阶段及分组证据。 |
| `WorkflowBinding` / `media_workflow_binding` | 向压缩项目公开的源/输出绑定、组/代次/目录版本。 |
| `ResourceLease` / `media_resource_lease` | 跨进程 GPU 租约、工作类型、进程身份/心跳、预算与显存观测。 |

`FeatureBundle.relative_path` 和 `ModelRun.relative_path` 是为历史兼容保留的字段；新摘要和新训练参数以数据库二进制字段为准。实际准确率由 evaluation 根据数据库历史记录计算，不是另一个“准确率文件”。

### 7.5 仓库外的运行依赖

| 依赖 | 使用位置与职责 |
| --- | --- |
| autoflow Python 环境 | 提供 Flask/SQLAlchemy/APScheduler、NumPy/scikit-learn、PyTorch/timm/transformers/torchaudio、openSMILE 等安装包；服务与 worker 使用该环境。未展开本机安装目录。 |
| openSMILE 包内 `core/config/**/*.conf*` | 真正的 eGeMAPSv02 配置及 include 树，位于已安装包而非项目 weights；provision/acoustic/cache 对其身份或摘要进行核验。 |
| openSMILE 原生运行库 | CPU 声学提取的原生实现，由安装包提供；不是项目里单独保存的模型权重。 |
| FFmpeg / ffprobe | 媒体探测、NVDEC 视频采样、CPU 音频解码以及压缩编码；路径由配置获取，不在本文展开。 |
| NVIDIA 驱动 / CUDA runtime / nvidia-smi | 提供 NVDEC/CUDA 计算和驱动显存/进程/利用率查询；GPU 资源管理从驱动采集信息。 |
| 数据库服务及驱动 | 保存共享 ORM 表；迁移和运行时通过同一个配置入口连接。 |
| 操作系统临时目录 | stderr 临时文件和离线测试的临时数据库/媒体/状态；正常由上下文清理，不是生产训练存储。 |

正常推理读取本地权重，不需要 Hugging Face 在线模型缓存；当前适配器按显式本地文件加载。显式 `provision.py` 才联网准备产物。

## 8. 重新分类时需要一起审查的职责交界

| 当前分散位置 | 需要区分的职责 |
| --- | --- |
| media_lineage 的 `service/files/workflows`、`resources`、`process_tree`、`integration` | 分别是血缘/文件身份/绑定、GPU 仲裁、进程基础设施、压缩适配；包名不足以覆盖所有实际职责。 |
| video_filter 的 `runtime` 与 `supervisor` | 前者决定业务工作及执行分支，后者决定派发、并发、生命周期和资源；二者都涉及“运行”但层级不同。 |
| `worker_client` 与 `persistent_client` | 前者定义一次任务的输入/输出协议及统一入口，后者管理跨任务复用进程的池。 |
| `worker`、`inference_worker` 与 `persistent_worker` | 前两者实现各任务的执行，后者仅提供可复用命令循环；一次性和常驻方式共享 execute。 |
| `extraction`、`batched_extraction`、`features/batching` | 单视频结果契约、跨窗口的模态批次组织、单模型批推理/OOM 重试三个层次。 |
| `configuration`、`group_config`、`training_config`、外部 `env.py` | 目录版本持久化、分组配置校验、模型训练契约、环境变量加载四种职责，不能当成重复配置文件。 |
| `model_registry`、`model_cache`、`features/manifest` | 个人分类器激活版本、进程内模型对象复用、冻结提取器本地文件完整性三个不同对象。 |
| `feedback`、`evaluation`、`reporting`、`progress`、`observability` | 用户决定入库、预测实际评价、展示数据整合、即时阶段文件、日志/错误传输各有自己的数据权威性。 |
| `feature_store`、DB `model_blob`、`weights`、state 临时 NPZ | 用户视频摘要、个人分类器参数、通用预训练模型、进程传输分别独立；文件后缀相同不表示存储职责相同。 |
| `video_filter/process_tree.py` 与公共实现 | 筛选侧是兼容入口，公共侧是唯一真实实现；改位置时需要检查双方和压缩消费者。 |
| media_lineage 测试依赖 video_filter 测试 support | 生产代码通过公共契约协作，但测试基础设施当前反向复用筛选侧，分类时也应考虑。 |
| 公共 dashboard 外壳与筛选 dashboard 页面 | 导航、样式、刷新通用能力在外壳，分组/模型/GPU 业务展示在筛选页面。 |
| weights 下的 BEATs 源码与业务适配器 | 前者是下载并校验的第三方实现，后者是本项目封装；当前一个被忽略、一个受源码管理。 |

如果后续移动文件，除了 imports，还需要同步检查显式注册、Alembic 元数据加载、`python -m` worker 模块名、Jinja 模板/static 路径、默认状态路径及 Git 忽略规则；本次只完成职责盘点。
