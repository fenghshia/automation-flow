# Automation Server 专项规则

适用于 `automation-server/**`。开始修改前同时遵循根目录 `AGENTS.md`。

## 架构与注册

- `app.py` 创建共享的 Flask `app`、Flask-SQLAlchemy `db` 和 Flask-APScheduler `scheduler`；修改初始化顺序前先检查所有导入方。
- 当前小项目通过包导入产生路由、模型和定时任务注册副作用。新增或移动模块时，检查该项目各级 `__init__.py`、`main.py`、`init_db.py` 和 `migrations/env.py`，确保需要的符号会被加载且不会重复注册。
- 保持小项目彼此独立。禁止从一个业务项目直接导入另一个业务项目的内部模块；真正通用的能力应在确认至少有两个调用方后再提取到明确的公共模块。
- 不为新代码扩大 `import *` 的使用范围。维护既有注册链时可以沿用，但应通过 `__all__` 明确导出，避免顺手进行全仓导入重构。

## 运行时配置与私有文件

- `automation-server/env.py` 中的 `EnvConfig` 是运行时配置的唯一入口；业务模块不得再次调用 `load_dotenv()`、直接解析 `.env`，或另建平行配置类。新增配置时扩展 `EnvConfig`，由调用方使用语义明确的方法获取。
- 真实标量配置只写入已忽略的 `automation-server/.env`；`automation-server/.env.example` 必须同步新增同名变量，但秘密、身份信息、私有地址和本机路径只使用明显占位值。
- 长文本规则、简历及其他不适合环境变量的私有内容统一放在已忽略的 `automation-server/private/`；源码只保存对应的环境变量路径，并由 `EnvConfig` 负责以 UTF-8 读取、缺失与空内容校验。
- 应用和 Alembic 共用 `EnvConfig.database_uri()`。`alembic.ini` 不保存真实 `sqlalchemy.url`，由 `migrations/env.py` 在运行时注入。
- 外部 CLI 的凭证通过子进程环境传递，不得拼入命令参数、脚本正文或日志。环境变量使用带业务前缀的 `UPPER_SNAKE_CASE`，避免含义模糊的通用名称。

## API（`<project>/apis/`）

- API 模块只负责 HTTP 输入输出、调用业务逻辑和事务边界，不承载浏览器或调度器专属实现。
- 延续现有项目的路由前缀和响应约定；改变 URL、方法、字段或状态码前先检查 Firefox 扩展、用户脚本及其他本地调用方。
- 即使接口无需鉴权，也要校验必要字段和类型，返回可诊断但不泄密的错误。不要记录敏感请求头、cookie、token 或完整隐私数据。
- 数据写入必须有清晰的提交点；异常路径按需要回滚 session，避免留下部分更新。

## 数据模型与迁移（`<project>/models/`、`migrations/`）

- 模型继承共享 `db.Model`，表名、nullable、默认值、索引和状态含义应明确；不要擅自重解释已有状态值。
- 模型变更需要同步评估 API、定时任务、现有数据和 Alembic 迁移。不得只改模型后宣称数据库已更新。
- 迁移命令在 `automation-server/` 中运行：`mamba run -n autoflow alembic revision --autogenerate -m "<message>"` 与 `mamba run -n autoflow alembic upgrade head`。
- 生成迁移后必须人工检查 upgrade/downgrade 和数据损失风险。除非用户明确要求且数据库目标清楚，只生成或审查迁移，不执行 `upgrade`；不要改写已应用迁移来代替新迁移。

## 定时任务（`<project>/schedules/`）

- 每个任务使用稳定且全局唯一的 scheduler id，并明确触发器、时间参数和 `misfire_grace_time` 等行为。
- 任务访问 Flask 扩展时必须在应用上下文中运行。数据库操作应具备明确的提交或回滚路径。
- 按“任务可能重复、重叠或在提交后崩溃”设计：尽量幂等，必要时先原子认领记录，并避免多个 scheduler 进程重复处理同一任务。
- 所有网络与外部程序调用设置超时，处理可预期异常，并让任务状态能够重试或人工诊断；不得在日志中输出秘密。
- 测试不得等待真实调度周期。将核心逻辑提取为可直接调用的函数，并在隔离的 app context、临时数据库和 mock 外部调用下验证。

## 运行与验证

- 本地服务命令：在 `automation-server/` 下运行 `mamba run -n autoflow python main.py`。只有人工联调需要时才启动；自动验证不要留下后台服务或活动 scheduler。
- 优先运行目标模块已有测试。当前没有统一测试配置时，不虚构测试命令；至少执行受影响 Python 文件的编译检查，并对路由、事务、任务幂等性或迁移做针对性验证。
- 测试不得连接真实数据库、处理真实任务、写入固定本机目录或调用真实第三方服务。
- 配置改动至少验证：受影响文件可编译、`EnvConfig` 在不打印实际值的情况下可读取配置，以及 `.env`/`private/` 仍被 Git 忽略。外部客户端初始化或命令执行使用假值和 mock 验证，不以真实调用代替配置测试。
