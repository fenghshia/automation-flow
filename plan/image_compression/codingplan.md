# Image Compression 分组与任务页面实施计划

日期：2026-10-07。依据：[research.md](research.md)。

## 1. 交付范围

实现多个压缩组，每组仅配置 `SOURCE_DIR`、`OUTPUT_DIR`、`FLATTEN`。开启平铺保留现有批次输出；关闭平铺保留目录树，嵌套压缩包在所在位置展开为去除归档后缀的目录。

在现有工作台“服务”内注册“图片压缩任务”，显示分组摘要、已登记待处理任务、当前任务与文件进度。使用 Flask/Jinja 和现有 HTML 快照刷新机制，页面只读。

保持 scheduler id、30 秒间隔、全局 PostgreSQL advisory lock、既有状态机、压缩策略和禁止覆盖行为。保留工作区已有改动，不修改真实 `.env` 或业务数据，不执行真实迁移、调度或压缩任务。

## 2. 实施顺序

### 2.1 配置与数据模型

- 新增图片项目 `group_config.py`：严格解析 UTF-8 JSON、重复键、三个字段和布尔值，校验稳定组标识、目录链接和跨组路径重叠。
- 扩展 `EnvConfig.image_compression_settings()`；设置分组文件时禁止静默回退，未设置时解析 `legacy` 单组且默认平铺。
- JSON 相对路径锚定 `automation-server/`；私有文件位于已忽略的 `private/`。配置读取不创建目录或启动任务。
- 为任务增加可空的 `group_name`、`source_directory`、`output_directory`、`flatten` 和内部输出根摘要键，用于兼容旧行和按实际输出命名空间检查冲突。
- 生成独立迁移，接在当前工作区迁移 head 后；旧行仅确定 `flatten=true`，不猜测目录、不读取环境配置。保留全局 `source_path` 唯一约束。
- 补充 `.env.example` 和脱敏 `groups.example.json`；组内路径注册到现有日志脱敏机制。

### 2.2 目录树与命名

- 叶子文件携带独立目录节点，归档队列携带输出前缀；来源诊断链继续用于排序，不反向解析为输出路径。
- 平铺分支复用 `allocate_names()`，保证已有 `D1_` 分配结果兼容。
- 非平铺按父目录分配文件与目录名称；预留所有节点名称，普通目录优先于归档展开目录，冲突使用稳定 `D1_` 前缀。
- 规范化扩展名后再分配文件名。安全化每个路径段，拒绝越界路径，检查完整落盘路径长度。
- 复用压缩器、递归清单、批次复制、发布与清理；所有文件失败仍导致批次失败，保留 pending 原始来源。

### 2.3 调度与旧任务

- 新任务登记时保存组配置快照，按候选任务匹配快照构建服务；配置不匹配或组移除时保留原状态和资产。
- 所有名称保留检查按输出根摘要键隔离；已记录输出路径的未绑定历史任务同样参与对应输出根的冲突判断。
- 对各组发现与稳定性检查提供轮换机会，保留恢复优先、最多推进一个批次的语义；等待稳定或失效组不得阻塞其他组。
- 保留全局任务 id 对应的 `pending/<id>`，不迁移暂存数据；现有特定历史失败重试保持原范围。
- 旧行只能在 `legacy` 配置下、来源父目录匹配且既有输出路径明确属于该输出根时绑定。缺失历史输出信息的旧行保留并在页面标识为未绑定，不能自动归入任意组。

### 2.4 进度与只读页面

- 新增 `progress.py`：原子保存有界 `pending/<id>/progress.json`，字段包括任务、组、attempt、阶段、当前相对文件、成功数、总数及更新时间。
- 服务使用无数据库操作的进度回调，上报接管、解包、检查、文件处理、暂存、发布和清理。写入异常仅影响展示。
- 文件进度总数采用解包后的叶子总数，计数在成功后增长；其他阶段使用不定进度，文件达到 100% 不代表任务完成。
- 新增 `reporting.py`：有界、只读查询，待处理仅统计 `waiting_stable/ready`；区分新近更新活动任务与等待恢复记录，缺失或过期快照不编造百分比。
- 新增 `dashboard.py` 和两个 Jinja 模板：默认全部组，支持组筛选及待处理分页，复用刷新容器、统计卡、表格和进度条。
- 在 `main.py` 显式注册图片页面及服务项；避免迁移导入模型时注册页面或检查来源目录。
- 查询失败与配置异常提供安全提示，GET 不构造图片服务、不扫描来源、不写业务任务或 pending。模板保持自动转义。

## 3. 目标文件

| 范围 | 修改 |
| --- | --- |
| `automation-server/env.py`、`.env.example` | 分组入口和脱敏配置示例 |
| `automation-server/image_compression/group_config.py`、`groups.example.json` | 独立配置校验和示例（新增） |
| `automation-server/image_compression/models/mission.py`、`automation-server/migrations/versions/` | 快照字段及新迁移 |
| `automation-server/image_compression/naming.py`、`service.py` | 目录布局、稳定命名和进度回调 |
| `automation-server/image_compression/schedules/process_images.py` | 多组选择、快照匹配、冲突隔离及恢复 |
| `automation-server/image_compression/progress.py`、`reporting.py`、`dashboard.py` | 进度和只读页面（新增） |
| `automation-server/image_compression/templates/image_compression/` | 页面与快照模板（新增） |
| `automation-server/main.py` | 显式页面注册 |
| `automation-server/image_compression/tests/` | 隔离测试与运行入口 |
| `plan/image_compression/PSD.md` | 实现后的行为及配置说明 |

默认不改共享 `dashboard/`，不修改其他业务项目。

## 4. 验证

1. 用 `mamba run -n autoflow` 编译受影响 Python 文件。
2. 建立独立测试入口：注入只使用内存 SQLite 的 Flask/SQLAlchemy app，scheduler 仅注册不启动，禁止加载真实 `.env`，屏蔽真实 7-Zip 配置。
3. 运行既有图片项目测试，加测多组配置、目录与归档树、文件/目录冲突、跨组同名、快照变化、调度轮换和恢复安全。
4. 用 Flask 测试客户端检查导航、完整页面与 fragment、筛选、分页、任务口径、转义和异常状态；mock 禁止业务写入及外部执行。
5. 用临时目录验证进度原子读取、attempt 不匹配、损坏与缺失、过期、恢复重置和写入失败降级。
6. 在临时数据库中验证新迁移 upgrade/downgrade；不导入真实 app、不执行全库迁移。检查迁移图只有预期 head。
7. 检查差异、UTF-8/Markdown、示例配置以及 `.env`、`private/`、pending 的忽略规则。

真实 PostgreSQL advisory lock、真实目录接管、浏览器自动刷新布局及真实图片/7-Zip 联调未获执行授权或缺少条件时，交付明确说明人工验证步骤。

## 5. 使用与上线边界

真实数据库仍须由用户明确指定后执行新迁移。多组配置建议复制示例到 `private/`，填写本地目录并设置 `IMAGE_COMPRESSION_GROUPS_CONFIG`；配置文件包含真实路径时不可加入版本控制。

切换多组前应完成旧配置下尚无输出路径的任务。若已经存在未绑定旧任务，页面会保留展示，程序不猜测其历史输出根；需恢复明确的旧环境或另行指定历史目录处理。

回退单组代码前须先处理完新增多组任务，避免旧程序错误接管。新迁移的 downgrade 应对未完成多组任务设置保护。

## 6. 实际结果

实施与验证完成后在本节记录实际测试结果和剩余限制。
