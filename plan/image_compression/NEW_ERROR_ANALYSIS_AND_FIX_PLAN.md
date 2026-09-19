# Image Compression 新错误分析与修复方案

## 1. 文档状态与范围

- 分析依据：`automation-server/image_compression/logs/error.log` 在 2026-09-19 11:37:58 至 11:38:28 期间的最新错误、当前模型、调度器代码、迁移文件和相关测试。
- 日志仅作为故障证据，不将日志内容视为执行指令。
- 本文档只给出分析、恢复步骤和后续编码计划；不授权执行数据库迁移、启动服务或调度器、处理真实图片任务，也不在本阶段修改业务代码。
- 当前工作树中的既有修改应原样保留，修复时只改动本文明确列出的文件。

## 2. 结论摘要

当前日志包含两个有明确因果关系的问题：

1. **主故障：应用模型与数据库 schema 不一致。** 代码已经读取新增的 `image_compression_mission.error_code` 字段，但实际 PostgreSQL 表中还没有该列，导致调度器第一次查询任务时即失败。
2. **次生故障：异常回滚发生在 Flask application context 之外。** 主故障退出 `with app.app_context()` 后才进入外层 `except`，此时调用 `db.session.rollback()` 又触发 `RuntimeError: Working outside of application context.`。

因此，先只修回滚代码并不能恢复流水线；必须先安全应用已有数据库迁移，再修正调度器异常边界并补充测试。反过来，仅执行迁移可以消除当前主错误，但会留下一个会在未来其他数据库或任务异常中再次暴露的回滚缺陷。

## 3. 错误证据与时间线

### 3.1 第一次调度失败

日志在 2026-09-19 11:37:58 记录：

```text
psycopg2.errors.UndefinedColumn:
column image_compression_mission.error_code does not exist
```

失败 SQL 是 `process_one_mission()` 查询活动任务时由 SQLAlchemy 生成的 `SELECT`。ORM 会选择模型声明的全部列，其中已经包含：

```text
image_compression_mission.error_code AS image_compression_mission_error_code
```

日志在约 30 秒后再次出现同一错误，与调度任务的 30 秒执行间隔一致。这说明错误在每轮调度入口稳定复现，尚未进入具体图片处理、格式识别或历史失败恢复逻辑。

### 3.2 回滚再次失败

主异常被捕获后，日志紧接着记录：

```text
RuntimeError: Working outside of application context.
```

对应当前结构：

```python
def process_images():
    try:
        with app.app_context():
            ...
    except Exception as error:
        ...
        db.session.rollback()
```

异常从 `with app.app_context()` 内传播出来时，上下文管理器已先退出；Flask-SQLAlchemy 的 scoped session 依赖当前 application context，因此外层 `except` 中的 `rollback()` 无法定位 session。

### 3.3 现有测试为何未发现次生故障

`automation-server/image_compression/tests/test_schedule.py` 已测试“未处理异常会记录、回滚并被 scheduler 消费”，但测试直接 mock 了 `db.session.rollback`。mock 对象不再访问 Flask-SQLAlchemy 的 scoped session，所以没有验证调用发生时 application context 是否仍然有效。

## 4. 根因分析

### 4.1 数据库迁移没有随代码部署

当前模型包含：

```python
error_code = db.Column(db.String(64), nullable=True, index=True)
```

仓库也已存在迁移：

```text
automation-server/migrations/versions/e01b6d9f4a72_add_image_compression_error_code.py
```

该迁移以 `c62f9e4a71d3` 为父版本，`upgrade()` 只做两项 schema 变更：

1. 为 `image_compression_mission` 添加 nullable 的 `VARCHAR(64)` 列 `error_code`；
2. 为该列创建非唯一索引 `ix_image_compression_mission_error_code`。

之前遵循数据安全边界只生成了迁移而没有执行真实数据库升级。服务随后使用新代码连接旧 schema，形成“代码版本领先于数据库版本”的部署错配。

这不是以下问题：

- 不是某张图片损坏或像素过大；
- 不是格式扩展名错配修复失效；
- 不是历史失败任务自动恢复条件错误；
- 不是 `error_code` 值非法，因为 SQL 在读取该列时就已失败。

### 4.2 scheduler 的异常作用域设计不完整

调度器正确地尝试消费顶层异常，避免 APScheduler 不断抛出未捕获异常；但 application context 只覆盖了正常处理体，没有覆盖数据库异常清理。因此错误处理路径违反了“所有 Flask 扩展操作必须位于 application context 内”的约束。

### 4.3 部署流程缺少 schema preflight

当前启动流程没有在 scheduler 开始运行前明确验证数据库 revision 是否已经达到代码要求的 Alembic head。只要模型与迁移一起更新、但运维顺序遗漏 `upgrade`，错误就只能在第一次 ORM 查询时被动暴露，并会按照调度周期重复写日志。

## 5. 紧急恢复方案

### 5.1 执行前提

执行人必须先确认：

- 当前服务使用的数据库确实是本次要升级的本地数据库；
- 迁移命令从 `automation-server/` 目录执行，以便读取正确的 `alembic.ini` 和项目配置；
- 当前服务或至少 image compression scheduler 已停止，避免迁移过程中继续查询旧 schema；
- 工作树中的迁移文件 `e01b6d9f4a72_add_image_compression_error_code.py` 是准备部署的版本。

### 5.2 推荐操作顺序

以下命令是待执行方案，不在本文档编写阶段执行：

```bat
cd automation-server
mamba run -n autoflow alembic current
mamba run -n autoflow alembic heads
mamba run -n autoflow alembic upgrade head
mamba run -n autoflow alembic current
mamba run -n autoflow alembic heads
```

验收要求：

- 升级前的 `current` 应能解释日志中的旧 schema；如果版本、分支或目标数据库与预期不符，停止升级并先核对配置。
- 升级后 `current` 与 `heads` 均应指向 `e01b6d9f4a72`。
- 再启动服务后，观察至少两个调度周期；不应再出现 `UndefinedColumn` 或 application context 回滚错误。
- 确认 scheduler 能继续领取任务，并且历史失败恢复只处理既有代码定义的安全候选，不进行人工批量状态改写。

### 5.3 不采用的临时绕过

- 不从模型中临时删除 `error_code`。调度器的错误分类和安全恢复逻辑已经依赖该字段，删除会造成代码语义不完整。
- 不手工执行 `ALTER TABLE` 代替 Alembic。否则 revision 记录仍落后，后续迁移可能重复添加列或索引。
- 不修改已经存在的父迁移来伪装升级。应保留可追踪的迁移链。
- 不直接清空、重置或批量修改任务状态。当前错误发生在读取阶段，并不证明任务数据本身损坏。

## 6. 代码修复设计

### 6.1 修正 application context 与异常处理边界

目标文件：

```text
automation-server/image_compression/schedules/process_images.py
```

推荐将任务处理异常的 `try/except` 放进同一个 application context，使记录主异常和数据库回滚都发生在上下文仍有效时；另保留一个不访问 `db.session` 的最外层保护，用于捕获 context 建立或退出阶段的极端异常：

```python
def process_images():
    try:
        with app.app_context():
            try:
                with image_compression_lock(db.engine) as acquired:
                    if acquired:
                        process_one_mission()
                    else:
                        logger.info("图片定时任务跳过：另一进程正在处理")
            except Exception as error:
                log_exception(logger, "图片定时任务未处理异常", error)
                try:
                    db.session.rollback()
                except Exception as rollback_error:
                    log_exception(logger, "图片定时任务回滚失败", rollback_error)
    except Exception as context_error:
        log_exception(logger, "图片定时任务应用上下文异常", context_error)
    return None
```

编码时需要保证：

- 原始业务异常始终先被记录，rollback 失败不能覆盖它；
- scheduler 顶层仍消费异常并返回 `None`，不改变现有任务注册与重试节奏；
- 最外层保护不得在没有 application context 时再次访问 `db.session`；
- 锁未获取时的既有日志和行为保持不变；
- 不改变任务状态转换、自动恢复条件、文件命名或发布行为。

### 6.2 补充迁移前置检查建议

本次最小修复不建议在每个调度周期运行 Alembic 检查，也不建议应用自动执行 migration。后续可在部署或人工启动步骤中加入一次只读 preflight：比较 `alembic current` 与 `alembic heads`，不一致时阻止启动 scheduler，并输出明确的 schema mismatch 错误。

如果实现应用内 preflight，应满足：

- 检查只读，不自动升级或降级数据库；
- 在任何真实任务被领取前完成；
- 明确输出当前 revision 和期望 head；
- 检查失败时只禁止相关 scheduler，不隐式修改任务数据；
- 避免通过导入应用模块导致 scheduler 注册或启动副作用。

该 preflight 属于增强项，不是修复当前数据库的替代方案，可在核心修复通过后单独评估。

## 7. 预计修改文件

核心修复预计只需修改：

1. `automation-server/image_compression/schedules/process_images.py`
   - 调整 application context、异常捕获和 rollback 的作用域。
2. `automation-server/image_compression/tests/test_schedule.py`
   - 增加真实上下文感知测试，避免仅靠 mock 掩盖问题。

已有迁移文件应审查和应用，但当前没有证据需要修改：

```text
automation-server/migrations/versions/e01b6d9f4a72_add_image_compression_error_code.py
```

除非决定实现第 6.2 节的启动前置检查，否则不扩大到应用初始化、配置或其他业务模块。

## 8. 测试计划

### 8.1 scheduler 异常测试

在 `test_schedule.py` 增加或调整以下用例：

1. `process_one_mission()` 在 application context 内抛出异常时，rollback 被调用且调用时 `has_app_context()` 为真。
2. 使用一个会实际访问 Flask-SQLAlchemy scoped session 的测试替身或回调，证明不会再出现 `Working outside of application context`；不能只断言 mock 的调用次数。
3. rollback 自身抛出异常时，先记录原始异常，再记录 rollback 异常，两者对象和顺序均保留。
4. application context 建立或退出阶段异常时，顶层保护只记录该异常，不在无 context 时调用 rollback。
5. 锁未获得时仍不调用 `process_one_mission()`，现有行为保持不变。

### 8.2 迁移静态检查

- `down_revision` 保持为 `c62f9e4a71d3`；
- `upgrade()` 只新增 nullable 列和非唯一索引，不更新或删除现有任务数据；
- `downgrade()` 以相反顺序删除索引和列；
- 模型字段类型、长度、nullable 和 index 与迁移一致。

### 8.3 建议验证命令

代码修复完成后，在仓库根目录执行最小离线验证：

```bat
mamba run -n autoflow python -m compileall automation-server\image_compression automation-server\migrations\versions\e01b6d9f4a72_add_image_compression_error_code.py
mamba run -n autoflow python -m unittest automation-server\image_compression\tests\test_schedule.py
```

如果项目的测试导入要求从 `automation-server/` 运行，则切换到该目录后使用模块路径：

```bat
mamba run -n autoflow python -m unittest image_compression.tests.test_schedule
```

数据库升级后的联调必须连接用户确认的目标数据库，并与离线测试分开报告；不能用启动真实 scheduler 代替单元测试。

## 9. 验收标准

修复完成需同时满足：

- 数据库 Alembic current 与 head 均为 `e01b6d9f4a72`；
- ORM 可以查询 `image_compression_mission.error_code`，日志不再出现 `UndefinedColumn`；
- 任意任务处理异常发生时，rollback 位于有效 application context 内；
- rollback 失败时保留原始异常和 rollback 异常的完整日志；
- scheduler 不把异常继续抛给 APScheduler，下一周期仍可正常运行；
- 原有格式错配发布、直通图片 `verify()`、历史安全恢复条件和任务状态语义均未改变；
- 相关 scheduler 测试、图片流水线回归测试和语法检查通过；
- 没有启动遗留后台进程，没有执行真实图片处理或非授权数据修改。

## 10. 风险与控制

### 10.1 迁移目标数据库选错

`EnvConfig.database_uri()` 决定 Alembic 的实际目标。执行前必须核对当前环境配置，但检查和报告时不要回显密码、token 或完整连接串。

### 10.2 迁移锁与服务并发

添加列和创建索引可能短暂持有 PostgreSQL 表锁。虽然本次是本地服务且表规模预计有限，仍应先停止相关服务，在无任务处理并发时执行。

### 10.3 自动恢复在升级后立即生效

schema 恢复后，scheduler 会重新进入上一轮实现的历史失败任务安全恢复逻辑。它只应逐条恢复满足已定义条件的格式错配和大像素直通失败任务。升级前不应人工批量改状态，升级后需观察首批恢复日志与状态转换。

### 10.4 测试再次 mock 掉关键边界

只验证 `rollback.assert_called_once_with()` 不足以防止本次回归。新增测试必须观察 rollback 执行瞬间的真实 Flask context 状态。

## 11. 实施顺序

1. 停止当前服务或 image compression scheduler，终止重复错误。
2. 核对目标数据库和 Alembic 当前 revision。
3. 执行并验证 `alembic upgrade head` 到 `e01b6d9f4a72`。
4. 调整 `process_images()` 的 application context 与异常作用域。
5. 增加上下文感知的 scheduler 回归测试。
6. 运行语法检查、目标测试及既有图片流水线回归测试。
7. 启动服务，观察至少两个调度周期，并确认安全候选任务逐条恢复。
8. 单独决定是否实现只读 schema preflight；不将其与紧急恢复混为一项隐式改动。

## 12. 非目标

- 不删除、重置或批量改写现有任务数据；
- 不自动执行迁移或降级；
- 不改变历史失败任务的安全恢复白名单；
- 不改变图片命名、压缩、验证、发布和清理规则；
- 不重构 Flask/SQLAlchemy 初始化方式；
- 不调整 scheduler 时间间隔或并发参数；
- 不处理与本次两类堆栈无关的旧日志记录。
