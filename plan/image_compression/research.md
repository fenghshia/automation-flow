# Image Compression 分组、目录结构保留与任务前端调研

调研日期：2026-10-07。

本文依据当前工作区代码记录现状、影响范围和建议方案。本次仅编写及更新调研文档，不修改运行时代码、真实配置或数据库，不执行图片任务。下文新增字段、配置格式、调度方式及前端页面均为建议，尚未实现。

## 1. 需求与范围

将 `automation-server/image_compression/` 从单组压缩调整为多组压缩。每组有三个业务配置项：

| 配置项 | 含义 |
| --- | --- |
| `SOURCE_DIR` | 该组来源目录 |
| `OUTPUT_DIR` | 该组成品目录 |
| `FLATTEN` | 是否将批次内文件平铺；`true` 平铺，`false` 保留目录结构 |

组标识用于配置索引、任务归属和诊断，不增加其他业务开关。7-Zip 目录、图片压缩策略、解包预算和调度周期继续共享。

同时在现有工作台的“服务”导航下增加“图片压缩任务”页面，展示待处理任务、当前处理任务及进度，并支持按组查看。

保持顶层对象作为批次的既有边界：目录、压缩包和散落文件分别形成独立任务。沿用现有压缩、复制、扩展名规范化、校验、禁止覆盖和成功后清理行为；新增页面只读，不增加任务控制或配置编辑功能，也不增加并发压缩、增量同步或跨项目导入。

## 2. 当前实现与证据

以下路径均相对仓库根目录，行号为调研时工作区位置。

| 文件与位置 | 当前职责及与需求的关系 |
| --- | --- |
| `automation-server/env.py:191` | `image_compression_source_directory()`、`image_compression_output_directory()` 只读取一对目录；7-Zip 目录为独立全局配置 |
| `automation-server/image_compression/schedules/process_images.py:70` | `build_service()` 构造单一目录配置的服务 |
| `automation-server/image_compression/schedules/process_images.py:84` | 来源发现只扫描直接子项，跳过任务隔离文件 |
| `automation-server/image_compression/schedules/process_images.py:109` | 登记任务；来源路径防重，输出名称冲突检查覆盖整张表 |
| `automation-server/image_compression/schedules/process_images.py:664` | 每轮先恢复活动任务，再尝试特定历史失败恢复、稳定性检查、领取和发现；所有查询均没有组范围 |
| `automation-server/image_compression/models/mission.py:17` | 任务没有组标识、输出目录快照或平铺设置；`source_path` 全局唯一 |
| `automation-server/image_compression/service.py:333` | `_collect_leaves()` 递归收集普通文件并展开压缩包；保留诊断来源链，未保存独立的输出相对路径 |
| `automation-server/image_compression/service.py:410` | `prepare_batch()` 对全部叶子文件统一分配文件名，写到 `result/<文件名>`，这是实际平铺位置 |
| `automation-server/image_compression/naming.py:67` | `allocate_names()` 按稳定来源顺序分配名称，使用 `D1_` 等前缀解决 Windows 规范化后的重名 |
| `automation-server/image_compression/manifest.py:95` | 成品清单递归记录相对路径和内容摘要，已有能力支持多层成品目录 |
| `automation-server/image_compression/service.py:610` | 成品复制至输出目录内 `.part` 暂存项，校验后按整个批次发布；目录批次已经使用递归复制 |
| `automation-server/image_compression/service.py:689` | 发布恢复依赖服务当前输出目录；若用错组服务，会检查或操作错误的暂存位置 |

### 2.1 平铺的准确边界

当前“根目录”是每个批次的成品根目录，不是将所有目录批次的图片混到全局 `OUTPUT_DIR`。

```text
SOURCE_DIR/album/a/photo.jpg  -> OUTPUT_DIR/album/photo.jpg
SOURCE_DIR/album/b/photo.jpg  -> OUTPUT_DIR/album/D1_photo.jpg
SOURCE_DIR/outer.zip         -> OUTPUT_DIR/outer/<全部叶子文件>
SOURCE_DIR/photo.jpg         -> OUTPUT_DIR/photo.jpg
```

建议 `FLATTEN` 只改变批次内部布局，保持这三类批次的最外层输出映射。

### 2.2 任务与恢复链

当前状态为：

```text
waiting_stable -> ready -> moving -> processing
               -> publishing -> cleanup_pending -> completed
处理错误 -> failed
```

接管时先在来源目录内隔离原对象，再复制、校验到 `image_compression/pending/<mission_id>/source`，写入接管标记，最后移除隔离副本。处理失败保留 pending 原始内容；成品确认完整后才清理 pending。

`publishing` 用数据库成品摘要恢复发布，`cleanup_pending` 校验最终成品后继续清理。历史失败自动恢复仅匹配现有的特定旧错误，不能借分组改造扩大为所有失败任务自动重试。

每 30 秒触发一次，scheduler id 为 `image_compression_process_one`，配置 `max_instances=1`、`coalesce=True`、`misfire_grace_time=120`；另有 PostgreSQL advisory lock 防止跨进程重入。分组不要求改变这些约定。

### 2.3 注册与消费者

注册链为 `main.py` → `image_compression/__init__.py` → `models/__init__.py` 与 `schedules/__init__.py` → `schedules/process_images.py`。`migrations/env.py` 同样通过包导入加载模型。仓库未发现 `automation-server/init_db.py`。

在 Python、Markdown、JavaScript 和 HTML 源文件中的引用检查显示，本项目的直接任务消费者为自身调度器；没有发现项目专属 HTTP API、浏览器插件或页面。共享日志系统识别项目包名，无须为分组更换日志目录。

已有建表迁移 `automation-server/migrations/versions/c62f9e4a71d3_add_image_compression_mission.py` 和错误码迁移 `automation-server/migrations/versions/e01b6d9f4a72_add_image_compression_error_code.py`。实现时须按当时完整迁移图生成新 revision，不采用旧设计文档记载的 head，也不改写已有迁移。

## 3. 建议的分组配置

### 3.1 统一入口与格式

建议新增 `IMAGE_COMPRESSION_GROUPS_CONFIG`，由 `EnvConfig.image_compression_settings()` 读取 UTF-8 JSON。真实文件置于已忽略的 `automation-server/private/`；仓库只保存使用虚构相对目录的 `image_compression/groups.example.json`，并同步 `.env.example` 的配置入口。

参考现有 `video_filter` 的“EnvConfig 入口 + JSON 校验”组织方式，但在图片项目内独立实现纯配置校验，不能直接导入另一个业务项目的 `group_config.py`。

建议使用对象键作为稳定组标识，让每组对象严格只包含三个业务项：

```json
{
  "schema_version": 1,
  "groups": {
    "flat_photos": {
      "SOURCE_DIR": "private/example-image-input-flat",
      "OUTPUT_DIR": "private/example-image-output-flat",
      "FLATTEN": true
    },
    "tree_photos": {
      "SOURCE_DIR": "private/example-image-input-tree",
      "OUTPUT_DIR": "private/example-image-output-tree",
      "FLATTEN": false
    }
  }
}
```

这些目录仅用于格式示例，不代表现有本机数据目录。相对路径统一锚定 `automation-server/`，沿用现有 `EnvConfig` 目录解析语义。

明确配置了分组文件时，必须严格读取；缺失文件、空组集合、错误类型、重复 JSON 键、未知字段或非法目录均应报配置错误，不能回退到旧来源继续接管数据。`FLATTEN` 必须为 JSON 布尔值，不接受字符串 `"false"`。

### 3.2 单组兼容与目录校验

建议未设置 `IMAGE_COMPRESSION_GROUPS_CONFIG` 时，将原 `IMAGE_COMPRESSION_SOURCE_DIR`、`IMAGE_COMPRESSION_OUTPUT_DIR` 映射为保留组 `legacy`，其 `FLATTEN=true`。分组配置不得使用这个保留标识，以免混淆旧任务。该兼容路径不增加第四个业务配置项。

配置校验应先完整完成，再创建输出目录或接管来源：

- 组标识长度受限，使用明确允许的字符集合，按大小写不敏感规则防重；不使用数组下标作为持久身份。
- 来源必须为既有目录，输出若已存在必须为目录；校验路径各级的符号链接与 junction，避免解析后扩大权限边界。
- 所有组的来源、输出与共享 pending 根目录必须互不相同、互不嵌套，考虑 Windows 大小写和既有目录别名；不能只调用每个 service 的组内三目录校验。
- 不允许 A 组输出落入 B 组来源，也不允许两组共享输出根。否则存在重复接管、任务隔离文件交叉解释及成品名称竞争。
- 分组 JSON 的位置、组内目录及其规范化路径需要接入现有日志脱敏机制；错误提示使用组标识和字段名，不回显原始 JSON 或本机路径。

## 4. 非平铺布局建议

### 4.1 普通目录和散落文件

`FLATTEN=false` 时，目录批次保留普通文件相对该批次源根的目录层级：

```text
SOURCE_DIR/album/a/photo.jpg -> OUTPUT_DIR/album/a/photo.jpg
SOURCE_DIR/album/b/photo.jpg -> OUTPUT_DIR/album/b/photo.jpg
SOURCE_DIR/album/docs/a.txt  -> OUTPUT_DIR/album/docs/a.txt
SOURCE_DIR/photo.jpg        -> OUTPUT_DIR/photo.jpg
```

非图片文件仍按现有策略复制，布局规则与图片一致。继续跳过 macOS 元数据；不增加空目录保留语义，无叶子文件的批次仍按现有规则失败。

### 4.2 压缩包目录映射

用户已明确普通目录需保留结构，但压缩包容器如何映射为目录未被单独指定。建议规则如下，作为后续实现的明确基线：

- 顶层压缩包仍去除完整归档后缀形成批次根，内部成员保留其相对目录。
- 批次内部的压缩包在原所在位置展开为同名目录，去除完整归档后缀，再递归保留成员目录。
- 不删掉压缩包原本自带的顶层目录，不将原始压缩包文件复制到成品。

```text
SOURCE_DIR/outer.zip!/a/photo.jpg
  -> OUTPUT_DIR/outer/a/photo.jpg

SOURCE_DIR/outer.zip!/a/inner.tar.gz!/b/photo.jpg
  -> OUTPUT_DIR/outer/a/inner/b/photo.jpg

SOURCE_DIR/album/packs/inner.zip!/c/photo.jpg
  -> OUTPUT_DIR/album/packs/inner/c/photo.jpg
```

开启平铺时，上述容器目录和成员目录继续全部省略，沿用当前全批次重名分配规则。此处非平铺的归档容器命名是建议方案，不应描述为现有行为。

### 4.3 数据表示与重名处理

为 `LeafFile`、`PreparedLeaf` 增加结构化的输出相对路径，并让归档队列携带输出目录前缀。保留 `provenance` 的现有排序与诊断职责，不从含 `!/` 的来源字符串反向解析真实输出路径，也不使用 `unpack/00000001` 等暂存目录名作为成品层级。

路径规划需要覆盖目录节点和文件节点：

- 平铺分支保留原 `allocate_names()` 行为和排序，确保原测试结果兼容。
- 非平铺分支在同一输出父目录内分配文件名；不同目录的 `photo.jpg` 均保留原名。
- 先按实际图片格式规范化扩展名，再做文件名冲突分配，沿用现有 `D1_` 前缀约定。
- 目录段同样应用 Windows 安全命名、Unicode NFC、大小写不敏感及长度限制；同名但不同来源的目录不能因规范化被无声合并。
- 预留普通目录、归档展开目录与文件占用的名称。普通 `inner/` 与归档 `inner.zip` 展开目录碰撞时，建议优先保留普通目录名，将归档目录分配为 `D1_inner/`；同父目录的文件与目录重名也须稳定分配，不能覆盖或到写入阶段才失败。
- 校验所有输出路径为 result 内的相对路径，拒绝绝对路径、盘符、`..`、链接或 junction；同时考虑完整路径长度，无法安全落盘时明确失败。

`archive.py` 已校验归档成员路径并限制解包深度和资源；这些保护继续保留。`compressor.py` 已创建目标父目录，`content_manifest()` 与目录发布已支持递归结构，主要改动应集中在路径规划，而非重写压缩或发布流程。

## 5. 任务归属、隔离与配置变更

### 5.1 建议保存任务配置快照

建议任务增加以下字段；它们是内部持久化数据，不是额外的用户配置项：

| 建议字段 | 用途 |
| --- | --- |
| `group_name` | 稳定组标识，用于候选查询与日志 |
| `source_directory` | 登记时规范化来源根，定位来源隔离副本 |
| `output_directory` | 登记时规范化输出根，定位最终成品与发布暂存项 |
| `flatten` | 登记时的平铺模式，处理中断后按同一布局重建 |

新任务登记时一次保存完整快照。`source_path` 不能替代完整快照：它只确定来源，无法确定尚未发布任务的输出目录和平铺模式。`output_path` 在 `publishing` 前才填写，同样不足以用于初期恢复。

继续保留全局 `source_path` 唯一约束及当前 `ON CONFLICT DO NOTHING` 防重，避免借此需求改变“同一源路径已登记后不做增量同步”的语义。分组来源根互不重叠，不需要为了分组就改成 `(group_name, source_path)` 唯一。

以下三处输出保留检查必须统一改为目标输出命名空间内的检查：`register_next_source()`、`_retry_destination_is_reserved()`、`_apply_prepared_destination()`。建议以规范化输出根和规范化批次名作为实际冲突键，并以组归属约束任务处理；不要仅按组名区分，否则同一组更换输出目录后，历史成品记录仍可能误阻塞新目录。

不同组的独立输出根允许相同批次名称；同一输出根仍保持现有禁止合并与覆盖的行为。包括扩展名规范化后的散落文件目标，也必须进入同一冲突检查。

### 5.2 pending 与恢复

保留 `image_compression/pending/<mission_id>/`：数据库 id 全局唯一，已能隔离组间任务；无须移动既有 pending 数据。来源隔离名、发布暂存名和批次根命名保持不变。

服务必须按候选任务的完整快照构造，不能先用任意当前组构造一个 service，再处理全表最早任务。稳定性检查、历史重试、发布恢复和清理均要核对任务所属组及其路径范围。

配置变更建议采用保守规则：当前组配置与任务快照不同，或组已移除时，不再推进该任务，不接管、不发布、不清理；记录组级配置不匹配诊断，保留任务原状态和资产，允许恢复匹配配置后继续。不要把配置不匹配统一写成普通 `failed`，以免破坏原恢复阶段。不匹配任务应从本轮可处理候选中排除，其他有效组继续工作。

改变 `FLATTEN` 只影响新登记任务。旧任务仍保存原模式；若其与当前配置不匹配，先恢复旧配置完成旧任务，再切换配置。特别是 `publishing`、`cleanup_pending` 阶段不能重新生成另一种布局或覆盖已发布结果。

### 5.3 旧数据兼容与迁移边界

旧任务缺少组配置快照，不能按照第一组、路径排序或任意当前输出根猜测归属。

建议新增迁移先允许快照字段为空，历史 `flatten` 按旧行为确定为 `true`，保留旧状态、摘要和路径。迁移本身不读取 `.env`、扫描业务目录或调用图片服务。

旧任务只在保留组 `legacy` 的旧配置明确可用、`source_path` 父目录匹配、已有 `output_path` 属于该输出根时绑定快照；绑定须事务化，完成后才进入正常任务处理。分组模式下未绑定旧记录保留原状态和 pending，不自动认领到新组；输出冲突检查对能由已记录 `output_path` 确认归属的旧成品仍需生效。

旧待处理任务尚无输出路径，单靠数据库无法确定历史输出根。切换分组前应使用旧配置完成这些任务，或后续另行设计明确指定旧目录的兼容绑定操作。不能从最新配置推断历史值，也不能通过删除任务或 pending 解决兼容问题。

本次未查看真实配置或连接数据库，因此无法确认是否存在待绑定旧任务。后续实现需要生成并检查迁移；执行迁移与处理真实任务需另有明确授权。降级到旧版单组代码前，应确认新增多组任务已经处理完毕；不能只删除字段后让旧程序接管剩余多组任务。

## 6. 多组调度建议

继续使用一个 scheduler job 和一个全局 advisory lock，串行处理，保持每轮最多推进一个批次。增加多组只改变候选范围和发现顺序，不需要每组注册独立 job。

建议在兼容现有活动任务恢复优先级的前提下，为有效组轮换发现和稳定性检查的机会；选择器须显式携带组范围，不能把单组流程简单包在按固定顺序循环的列表里。尤其当前等待任务分支会直接返回，不能让 A 组持续变化的来源阻止 B 组 ready 任务或来源登记。

组内服务初始化、来源扫描或配置匹配失败应回滚本组事务、记录组标识，并继续检查其他有效组；配置文件结构错误或跨组目录重叠则停止整轮。持续有任务的组间应有可验证的轮换策略；发现新来源可只登记任务，不算推进第二个批次。长批次仍会占用串行处理时间，本需求不引入并发执行。

日志增加 `group_name` 与 `flatten` 上下文，保留原 mission id、错误码和状态诊断。继续沿用共享日志配置与路径脱敏，不建立一套新的日志系统。

## 7. 图片压缩任务前端

### 7.1 现有工作台与接入方式

现有前端使用 Flask Blueprint、Jinja 模板和共享静态资源，已有可直接复用的服务注册与快照刷新机制：

| 现有文件 | 已确认的能力 |
| --- | --- |
| `automation-server/dashboard/__init__.py` | `register_service()` 注册服务标题、描述和页面 endpoint；同时注册共享工作台 Blueprint |
| `automation-server/dashboard/templates/dashboard/base.html` | 根据 `dashboard_services` 自动生成“服务”导航项，以 `active_service` 高亮当前页面 |
| `automation-server/dashboard/templates/dashboard/index.html` | 根据相同注册信息自动生成运行概览中的服务卡片 |
| `automation-server/dashboard/static/dashboard.js` | 每 5 秒请求 HTML 快照；支持立即刷新、暂停刷新、隐藏页面停止轮询、超时、失败退避及保留上次数据 |
| `automation-server/dashboard/static/dashboard.css` | 已有统计卡、任务表、进度条、空状态和响应式布局样式 |

图片项目应使用自己的 Blueprint、查询模块和模板，复用共享工作台；不导入 `video_filter` 的内部页面或进度模块。

建议注册项为 `key=image_compression`、`title=图片压缩任务`、`endpoint=image_compression_dashboard.index`。通过共享注册函数同时接入“服务”导航与概览卡片，页面设置 `active_service=image_compression`，无须在共享导航模板硬编码新链接。

建议页面路由为 `GET /image_compression/dashboard/`，快照路由为 `GET /image_compression/dashboard/fragment`。完整页面继承 `dashboard/base.html`，采用 `service-snapshot` 容器、`data-refresh-url`、`.snapshot-body`、`data-updated-at` 及共享刷新按钮约定；响应使用 `Cache-Control: no-store`。没有 JavaScript 时仍能查看服务端渲染的当前快照。

在 `main.py` 中显式导入并调用图片前端注册函数，Blueprint 和服务注册须可重复调用而不重复注册。模型与任务保持现有包注册链；不让 Alembic 导入模型时顺带读取分组目录、初始化图片服务或注册页面。

### 7.2 页面信息与任务口径

默认展示全部组，提供“全部组 / 指定组”筛选。筛选通过 `group` 查询参数传递，刷新、分页和跳转保持同一筛选条件；非法组标识返回明确错误，不能静默扩大到全部组。历史保留组或未绑定任务可单独标注展示，不能因此获得运行资格。

页面由以下区域组成：

| 区域 | 展示信息 |
| --- | --- |
| 概览 | 当前筛选范围内待处理总数、等待稳定数、已就绪数和恢复待续任务数；最后成功刷新时间 |
| 分组摘要 | 组标识、平铺或保留结构、待处理数量、活动阶段及配置异常提示；全部组视图下逐组展示 |
| 当前任务 | 任务 id、组、批次名及类型、平铺模式、处理阶段、当前文件相对路径、文件处理进度、尝试次数、开始时间及已用时 |
| 待处理列表 | 任务 id、组、批次名、类型、等待稳定或已就绪状态、来源快照文件数与大小、登记时间、等待时长 |

待处理总数仅统计已登记的 `waiting_stable` 和 `ready` 任务，配置不匹配者仍展示原状态并附“配置不匹配，等待恢复”的说明。页面注明“已登记任务”，因为当前发现逻辑每次只登记一个来源；不能将数据库数量描述为磁盘上的全部待处理对象数量，也不能为补齐数量在页面请求时扫描真实来源目录。

`moving`、`processing`、`publishing`、`cleanup_pending` 是活动或待恢复状态，应单独展示，不重复计入待处理列表；`completed`、`failed` 不计入待处理。本阶段不增加历史任务管理、重试、启动、暂停或删除任务按钮。“暂停刷新”只控制浏览器轮询，不改变调度器或任务状态。

正常串行执行时当前执行任务最多一个；中断或配置变化后，数据库可能存在多条活动记录。页面应区分有新鲜进度的当前任务与等待恢复的记录，不能把第一条活动状态记录直接认定为正在运行；进度新鲜也只表示最近有更新，不作为进程存活保证。无当前执行证据时展示“暂无正在执行的任务”，仍列出恢复待续记录。

待处理列表建议默认每页 20 条、最多 50 条，按登记时间和 id 稳定排序，并显示总数及分页。此列表顺序用于浏览，不承诺等同于考虑恢复优先级和组轮换后的精确执行顺序。

### 7.3 进度来源与展示语义

当前 `service.py` 在文件处理前后输出文件序号、总数与百分比，但模型没有可直接读取的实时进度字段。`source_file_count` 记录接管前来源快照：压缩包可能只计为一个文件，不能当作解包后处理总量；`output_file_count` 在准备发布后才写入，也不能支撑处理中进度。

建议在图片项目内增加轻量进度上报回调和独立 `progress.py`，在已忽略的 `pending/<mission_id>/progress.json` 保存可读快照。使用同目录临时文件与原子替换发布完整 JSON，避免轮询读到半写入内容；页面不解析运行日志，不将每个文件的进度写入任务事务。

建议快照至少包含 `mission_id`、`group_name`、`attempt`、`stage`、`current_file`、`completed_files`、`total_files`、`updated_at`。`current_file` 使用任务内相对路径，不暴露源盘符、输出根、pending 路径或原始异常内容。字段有明确类型、长度及文件大小限制。

| 阶段 | 展示方式 |
| --- | --- |
| 接管来源 `moving` | 显示“接管来源”，没有可靠字节总量时使用不定进度 |
| 解包、收集与检查 `processing` | 显示具体子阶段及当前归档或文件；叶子总数尚未确定时不显示百分比 |
| 逐文件处理 `processing` | 显示“已完成 N / 共 M 个文件”、当前文件和 `N / M × 100%` |
| 成品暂存与校验 `processing` | 显示“暂存与校验”；沿用当前暂存完成后才提交 `publishing` 的顺序 |
| 发布 `publishing` | 显示“发布成品”；100% 文件处理不代表整个任务已完成 |
| 清理 `cleanup_pending` | 显示“清理暂存数据”，直到数据库状态为 `completed` 才展示完成 |

解包完成并确定待处理叶子后设置总数；文件开始处理时更新当前文件，文件成功处理并校验后增加完成数。进度是文件数量比例，不承诺时间比例或预计完成时间；不把阶段数量平均分配成虚假的整体百分比。已有当前文件处理前后日志可继续保留。

进度快照仅用于观测，数据库状态和既有内容清单仍是任务恢复依据。为避免干扰压缩，阶段切换立即更新，连续文件更新可合并或节流；使用不涉及数据库 session 的上报回调，进度写入失败只影响展示并记录诊断，不改变任务状态或阻止已验证成品发布。

每次领取或恢复时重置本次 `attempt` 的进度；恢复处理中批次会重新生成结果，因此完成数可以从零开始，不沿用上次计数。读取端核对任务 id、组、attempt 和数据库状态；拒绝不匹配、越界、损坏或过大的快照。长时间没有更新时展示“进度暂未更新”，不能直接推断失败；旧任务没有快照时仍显示阶段和“进度暂不可用”，不编造 0% 或 100%。

任务完成、失败或进程中断后不得仅凭遗留快照显示为实时执行，终态以数据库为准，中断风险通过快照时间与“进度暂未更新”提示表达。完成清理可能移除 `progress.json`，这是正常情况；页面查询必须只读，不重新创建 pending 目录或清除文件。

### 7.4 只读查询与异常状态

建议由图片项目的 `reporting.py` 聚合数据库任务和有限的进度元数据，`dashboard.py` 负责校验筛选、分页参数与渲染。配置读取仍通过 `EnvConfig`，页面不构造会创建目录的 `ImageCompressionService`，不执行来源发现、任务登记、认领、历史绑定、重试或业务 commit。

轮询只做任务计数、有限列表查询和对应任务的进度快照读取，不计算文件摘要、不探测图片、不调用 7-Zip、不递归扫描来源或输出目录。进度路径由数值任务 id 和固定 pending 根构造并校验，不接受前端提供任意文件路径。

配置不可用、数据库结构尚未迁移或查询失败时显示明确的“数据暂不可用”提示，不将未知数量显示为零；异常路径按需回滚只读 session，并沿用日志脱敏。数据库可读时，即使配置不可用，也应尽可能展示已登记任务，并标明无法确认当前配置。首次无任务时显示空状态；后续刷新失败保留上次快照并提示数据可能已过期。

文件名与组名使用 Jinja 自动转义，错误摘要只展示安全错误码或经过处理的说明，不直接插入日志或 HTML。时间返回带时区的 ISO 值并按浏览器本地时间展示，复用共享脚本；读接口不改变本地运行的权限边界。

## 8. 后续最小改动范围

| 文件或范围 | 建议变更 |
| --- | --- |
| `automation-server/env.py` | 新增分组设置入口、兼容单组解析和相关路径脱敏注册 |
| `automation-server/.env.example` | 新增分组 JSON 路径示例，说明旧目录配置的兼容用途 |
| `automation-server/image_compression/group_config.py`（拟新增） | 纯 JSON 类型、组标识和全组路径校验；不导入 Flask，不启动调度器 |
| `automation-server/image_compression/groups.example.json`（拟新增） | 三项组配置的脱敏示例 |
| `automation-server/image_compression/models/mission.py` | 增加任务归属及目录、模式快照 |
| `automation-server/migrations/versions/` | 新增迁移，评估旧数据绑定和回退限制 |
| `automation-server/image_compression/service.py` | 传入平铺模式，携带相对路径，按模式规划目标；上报解包、检查及逐文件进度 |
| `automation-server/image_compression/naming.py` | 保留平铺分配，增加目录结构下的确定性命名与碰撞规划 |
| `automation-server/image_compression/schedules/process_images.py` | 按组登记、选择和恢复任务，统一输出命名空间检查，隔离组故障；初始化本次进度并上报接管、发布、清理阶段 |
| `automation-server/image_compression/progress.py`（拟新增） | 有界进度快照的原子写入、校验读取和失败降级；不改变任务事务 |
| `automation-server/image_compression/reporting.py`（拟新增） | 只读任务计数、分组摘要、有限待处理列表与当前进度聚合 |
| `automation-server/image_compression/dashboard.py`（拟新增） | 页面和 HTML 快照 Blueprint、筛选分页校验、服务注册函数 |
| `automation-server/image_compression/templates/image_compression/dashboard.html`（拟新增） | 继承共享工作台、组筛选及刷新容器 |
| `automation-server/image_compression/templates/image_compression/_snapshot.html`（拟新增） | 统计卡、分组摘要、当前任务进度、恢复提示和待处理表 |
| `automation-server/main.py` | 显式接入图片页面与“服务”注册 |
| `automation-server/image_compression/tests/` | 扩充路径结构、跨组冲突、配置快照与恢复测试；增加前端只读查询和进度快照测试 |
| `plan/image_compression/PSD.md` | 实现完成后同步当前产品行为；不在调研阶段把建议写成已实现功能 |

`archive.py`、`compressor.py` 和 `manifest.py` 原则上复用现有能力，只有针对性验证暴露必要缺口时才修改。保持现有包注册导出，配置解析不得在导入包时触发来源目录操作。共享 `dashboard/` 的注册、样式与刷新脚本已能支持本页面，默认直接复用；只在确有缺口时扩充公共样式，并保留工作区已有改动。

## 9. 验证与验收基线

当前已有 `unittest` 测试文件：`test_archive.py`、`test_compressor.py`、`test_manifest.py`、`test_naming.py`、`test_service.py`、`test_schedule.py`。服务测试已有临时目录、假的递归解包器和复制处理器；调度测试使用 mock 验证锁、失败回滚、恢复与历史重试，可在此基础上扩充。

后续实现至少验证：

1. **配置**：两组独立设置，严格布尔类型、重复键、空组集合、非法标识、路径重叠、链接目录、配置文件缺失不回退；旧配置单组默认平铺。
2. **布局**：平铺结果及 `D1_` 分配与现有测试一致；非平铺目录、顶层压缩包、嵌套压缩包、散落文件符合第 4 节映射。
3. **重名**：跨目录同名不改名；同目录扩展名规范化后的碰撞、目录大小写或安全化碰撞、归档目录与普通目录碰撞、文件与目录碰撞均不覆盖且结果确定。
4. **隔离**：A/B 组同名批次不误报冲突；同输出命名空间仍拒绝冲突；一组等待稳定、目录不可用或配置变化不阻止另一组有资格的任务。
5. **恢复**：分别在接管、处理中、发布前后和清理中模拟中断；按快照恢复，摘要一致，失败保留原始 pending，清理仅移除当前任务资产。
6. **旧记录**：已完成、待处理、发布中、清理中与历史失败记录分别覆盖；未绑定或配置不匹配记录不被错误组认领，旧成品保留检查仍有效。
7. **调度与注册**：job id、时间参数、全局锁保持兼容，多组轮换不会登记重复来源或在一轮推进多个批次。
8. **页面入口与筛选**：“服务”出现“图片压缩任务”，概览卡片能进入页面，导航正确高亮；全部组、指定组、非法组、历史未绑定任务和分页保持正确范围，重复注册不会冲突。
9. **任务口径**：待处理只统计 `waiting_stable`、`ready`；当前任务与恢复待续记录分开；完成、失败、配置不匹配、没有任务及数据库不可用均有正确展示，不把来源快照数当作解包后文件总量。
10. **进度**：临时目录与假处理器验证阶段、当前文件和成功完成计数；百分比仅用于已知叶子总数的文件处理；覆盖原子读取、损坏或缺失快照、attempt 不匹配、过期数据、恢复计数重置、写入失败不影响业务及成品发布后仍需清理的情况。
11. **只读与刷新**：Flask 测试客户端验证 GET 页面与 fragment 不登记、认领、绑定或 commit 业务任务，不创建业务目录或执行外部工具；HTML 转义和错误提示不暴露本机路径。浏览器人工检查每 5 秒刷新、暂停及立即刷新、后台页面停止轮询、刷新失败保留上次数据、无 JavaScript 和窄屏布局。

Python 命令使用 `mamba run -n autoflow`。后续先编译受影响文件，再运行隔离的相关测试；测试用临时目录、临时数据库或 mock，不连接真实数据库，不读取真实分组目录，不启动 scheduler。直接运行现有包测试会经过包导入和 `app.py` 配置初始化，必须先建立隔离的配置与数据库条件，不能把应用导入当作无副作用的语法检查。

本次验证仅针对调研 Markdown 的 UTF-8、标题、代码围栏、示例 JSON、现有引用路径和目标差异。未运行业务测试、浏览器联调、7-Zip、图片压缩、真实数据库检查或迁移；压缩包非平铺映射、多组调度、旧任务绑定和前端进度仍待后续实现验证。
