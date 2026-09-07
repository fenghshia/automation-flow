# Automation Flow 日志模块调研与方案建议

> 调研日期：2026-09-07  
> 适用范围：`automation-server/` 主服务及其 Python 业务子项目  
> 文档性质：技术调研与实施建议；本次不修改运行代码、不安装依赖、不执行数据库迁移或真实任务

## 1. 结论摘要

推荐采用 **方案 A：Python 标准库 `logging` + 中央配置 + 按 logger 名称分流**。

核心结构如下：

```text
automation-server/
├─ logging_config.py                 # 唯一日志初始化入口
├─ logs/
│  ├─ runtime.log                    # Flask、Werkzeug、APScheduler 等主服务日志
│  └─ error.log
├─ image_compression/logs/
│  ├─ runtime.log
│  └─ error.log
├─ video_compression/logs/
│  ├─ runtime.log
│  └─ error.log
├─ iwara/logs/
│  ├─ runtime.log
│  └─ error.log
├─ jd_auto_match/logs/
│  ├─ runtime.log
│  └─ error.log
└─ vps_data_backup/logs/
   ├─ runtime.log
   └─ error.log
```

每个业务模块只需增加：

```python
import logging

logger = logging.getLogger(__name__)
```

然后将 `print()` 替换为 `logger.info()`、`logger.warning()` 或 `logger.exception()`。子项目路径由模块名首段自动识别，例如 `image_compression.schedules.process_images` 自动进入 `image_compression/logs/`。主服务在 Flask app 创建前完成一次配置，现有路由、任务注册、数据库状态和业务调用链无需重构。

该方案的主要理由：

- 无新增依赖，兼容当前 Python 3.14、Flask 和 Flask-APScheduler 技术栈；
- Python logger 本身按点号形成层级，当前子项目恰好都是独立顶层包，天然适合分流；
- `logger.exception()` / `exc_info` 能将完整 traceback 写入文件，同时数据库仍可保留现有的 2,000 字符摘要；
- 文件 handler、错误级别 handler、格式、轮转和保留份数都可在一个模块统一控制；
- 子项目改动只限日志调用，不需要从业务包导入主应用对象，也不改变异常处理语义。

## 2. 当前项目现状

### 2.1 架构与入口

- [`automation-server/app.py`](../../automation-server/app.py) 创建共享 Flask app、SQLAlchemy 和 APScheduler，但没有日志初始化。
- [`automation-server/main.py`](../../automation-server/main.py) 通过导入各顶层包完成路由、模型和定时任务注册，然后在同一进程中启动 scheduler 与 Flask；`use_reloader=False`，当前默认运行形态是单进程。
- 已识别的业务子项目为 `image_compression`、`video_compression`、`iwara`、`jd_auto_match` 和尚未完整接入主入口的 `vps_data_backup`。
- Flask 默认会把应用日志写到 WSGI 错误流；APScheduler、Werkzeug、SQLAlchemy 等也使用标准库 logging，因此目前主要汇总到控制台。Flask 官方建议在创建 app 前完成 logging 配置。

### 2.2 现有日志问题的代码证据

仓库中共有 **21 处有效 `print()`，分布在 8 个 Python 文件**：

| 子项目 | 文件 | `print()` 数量 | 主要问题 |
| --- | --- | ---: | --- |
| `image_compression` | `schedules/process_images.py` | 1 | 捕获流水线异常后只打印截断错误文本，没有 traceback。 |
| `video_compression` | `schedules/compress_videos.py` | 1 | 多个异常入口汇总到 `fail_mission()`，最终只打印摘要。 |
| `iwara` | 4 个 API/调度文件 | 11 | 正常消息、错误消息和浏览器上报日志全部混在 stdout；部分错误仅打印异常字符串。 |
| `jd_auto_match` | 2 个 API/模型文件 | 8 | 打印完整请求数据；语义检索异常没有 traceback。 |

额外风险：

1. 图片与视频任务的数据库 `error_message` 有意限制为 2,000 字符，适合作为任务摘要，但不应承担完整故障诊断。
2. 图片和视频 scheduler 的顶层异常会回滚后重新抛出，最终通常由 APScheduler 以 `apscheduler.*` logger 输出，无法可靠进入对应子项目日志目录。
3. `iwara/apis/log.py` 接收浏览器传入的日志等级和任意文本，当前没有长度、等级或换行约束，直接落盘后可能造成日志伪造、文件快速增长或敏感信息进入日志。
4. `jd_auto_match/apis/score.py` 的 `print(data)` 可能记录完整职位描述等私有内容，不应直接迁移成 INFO 文件日志。
5. [`.gitignore`](../../.gitignore) 只忽略根层的 `error.log`，没有覆盖计划中的各子项目 `logs/` 目录。

## 3. 需求转化为设计约束

| 用户要求 | 设计约束 |
| --- | --- |
| 保存到日志文件 | 每个项目至少有持久化 file handler，并采用 UTF-8。 |
| 可读性强 | 单行包含时间、级别、项目/模块、进程/线程、源码位置和消息；异常后追加多行 traceback。 |
| 异常单独写入 error 文件 | 每个目录设置独立 `ERROR` 级别 handler。建议错误同时保留在 `runtime.log`，以便查看故障前后的完整时间线。 |
| 堆栈完整 | 在仍处于 `except` 上下文时调用 `logger.exception()`，或显式传入异常的 traceback；不能只记录 `str(error)`。 |
| 按子项目拆分 | 以 `LogRecord.name` 的首段匹配顶层包，不依赖调用者手工传项目名。 |
| 子项目目录内保存 | 固定为 `<subproject>/logs/runtime.log` 与 `<subproject>/logs/error.log`。 |
| 高集成、改动小 | 配置集中到一个公共模块；业务文件只声明模块 logger 并替换 `print()`。 |
| 可长期运行 | 启用按大小轮转和有限保留，避免无限增长。 |
| 公开仓库安全 | 整个 `logs/` 目录必须 Git ignore；不得记录 token、cookie、请求头、简历全文、完整 URL 查询参数和外部工具无限输出。 |

## 4. 候选方案对比

| 维度 | 方案 A：标准库 `logging` | 方案 B：Loguru | 方案 C：structlog + `logging` |
| --- | --- | --- | --- |
| 新依赖 | 无 | `loguru` | `structlog` |
| Flask/APScheduler 集成 | 原生，最直接 | 需要拦截/转发标准日志 | 仍需同时配置标准 logging |
| 子项目代码改动 | 小 | 小到中 | 中 |
| 按包名分流 | 原生 logger 层级 + filter/handler | sink filter 可实现 | 依赖底层 logging handler 或 processor |
| 文件轮转 | 标准 handler 支持 | 内建语法最简洁 | 通常仍由 logging handler 完成 |
| 完整异常堆栈 | 原生支持 | 支持且显示友好 | 支持，可文本或结构化输出 |
| 人工阅读 | 好 | 最好 | 好；JSON 模式人工阅读较差 |
| 结构化检索 | 中，可用 `extra` 扩展 | 中到好，可 `bind()` | 最强 |
| 多进程同文件 | 标准 FileHandler 不支持，需 Queue/专用 handler | `enqueue=True` 可增强完整性 | 取决于底层 handler |
| 配置复杂度 | 中，集中后稳定 | 低到中；桥接后上升 | 高 |
| 当前项目匹配度 | **最高** | 较高 | 现阶段偏重 |

## 5. 各方案优缺点

### 5.1 方案 A：Python 标准库 `logging`（推荐）

实现方式：在 `logging_config.py` 中创建 console handler、主服务文件 handler，以及每个子项目的 `runtime`/`error` handler；给顶层包 logger 配置对应 handler 并关闭向 root 的重复传播。业务模块统一使用 `logging.getLogger(__name__)`。

优点：

- Python、Flask、Werkzeug、APScheduler 和 SQLAlchemy 共用同一套标准接口，不需要日志桥接层。
- logger 名称与现有包结构一致，可稳定分流且能保留精确模块名。
- 不引入依赖版本与安装问题，符合当前项目“最小改动、集中公共能力”的规则。
- `RotatingFileHandler` 可按体积轮转并保留固定份数；`QueueHandler` / `QueueListener` 可作为未来异步或多进程升级路径。
- `logger.exception()` 本质上是 ERROR 日志并附带当前异常信息，能满足完整 traceback 要求。
- 可以逐文件迁移；未迁移的 Flask/第三方标准日志仍会进入主服务日志，不会形成两套体系。

缺点：

- 中央 handler/filter 配置比 Loguru 更啰嗦，需要测试“不会重复记录”和“不会串项目”。
- 标准 `RotatingFileHandler` 支持同一进程内多线程，但不支持多个进程安全写同一文件；将来若以多进程方式运行服务，需要改为集中 QueueListener 或经验证的并发轮转 handler。
- 默认格式不带彩色和自动上下文；`mission_id` 等字段需要通过消息参数、`extra` 或 `LoggerAdapter` 明确加入。

适用判断：当前入口是单进程，且最重要的是与现有 Flask/APScheduler 日志统一、降低子项目修改面，因此该方案最合适。

### 5.2 方案 B：Loguru

实现方式：为每个子项目注册两个 file sink，通过 `record["name"]` 或绑定的 `project` 过滤；启用 rotation、retention，必要时启用 `enqueue=True`；再增加 InterceptHandler，把 Flask/Werkzeug/APScheduler 的标准日志转入 Loguru。

优点：

- API 简洁，轮转、保留、压缩和文件 sink 配置直观。
- 异常输出默认更美观，`logger.catch`、`logger.exception`、`bind()` 使用方便。
- `enqueue=True` 可用于异步并增强多进程场景的日志完整性。

缺点：

- 项目现有框架和依赖仍使用标准 logging，必须维护拦截桥接；桥接配置不完整时容易重复、漏记或显示错误调用位置。
- Loguru 的 `diagnose=True` 可能把局部变量值带入异常日志，官方明确警告可能泄露敏感数据；本项目必须设为 `diagnose=False`。
- 引入新依赖，并将业务代码逐步绑定到第三方 API；以后回到标准 logging 的迁移成本更高。
- “全局单 logger + 多 sink”虽然简洁，但在本项目这种严格按包目录拆分的场景中仍要维护过滤规则，优势会缩小。

适用判断：如果非常看重漂亮的本地终端输出与极简配置，并愿意接受标准日志桥接，可选；不是当前最稳妥方案。

### 5.3 方案 C：structlog + 标准 `logging`

实现方式：业务日志使用事件与字段，例如 `logger.info("mission_completed", mission_id=..., output=...)`；structlog processor 添加时间、级别、模块、上下文和异常，最终由标准 logging handler 分流到文本或 JSON 文件。

优点：

- 结构化字段适合按 `mission_id`、状态、文件名、耗时检索和后续接入 Loki/ELK 等系统。
- 可为控制台使用易读文本，为机器采集使用 JSON；异常也可结构化保存。
- `contextvars`、bound logger 适合贯穿一次请求或任务的上下文。

缺点：

- 官方文档也指出与标准 logging 的完整整合初次配置较繁琐；需要同时理解 processor、formatter 与 handler。
- 当前项目只要求本地文件诊断，没有集中检索平台，JSON/事件建模的收益暂时有限。
- 要发挥优势，现有日志调用不能只做机械替换，还要设计事件名和字段，子项目改动面明显大于方案 A。
- 每个子项目的文件分流和轮转最终通常仍要依赖标准 logging handler。

适用判断：如果下一阶段已确定要接入集中日志平台、跨任务链路查询或统计分析，可直接选择；否则建议在方案 A 稳定后按需演进。

### 5.4 不建议只使用 `app.logger`

`app.logger` 也是标准 logging，但让所有业务模块都从 `app` 导入同一个 logger，会产生额外 Flask 耦合并丢失顶层包名带来的自动分流能力。它适合 Flask app 自身日志，不适合作为所有独立流水线的唯一接口。

## 6. 推荐方案的详细设计

### 6.1 初始化顺序

1. `app.py` 导入并调用 `configure_logging()`；
2. 完成日志目录创建和 handler 注册；
3. 再创建 `Flask(__name__)`、数据库与 scheduler；
4. `main.py` 再导入各业务包触发路由和任务注册。

这样即使数据库配置或业务包导入阶段失败，也有机会记录启动异常。初始化函数必须幂等，测试重复导入或 Flask 调试启动时不能叠加 handler。

### 6.2 路由规则

显式维护受支持的顶层包集合：

```text
image_compression
video_compression
iwara
jd_auto_match
vps_data_backup
```

每个顶层 logger 配置：

- `runtime` handler：`INFO` 及以上；
- `error` handler：`ERROR` 及以上；
- console handler：`INFO` 及以上，格式中必须显示项目/模块；
- `propagate=False`，防止同一业务日志再次写入 root 主服务文件。

root logger 接收 Flask、Werkzeug、APScheduler、SQLAlchemy 和无法匹配的日志，写入 `automation-server/logs/`。不建议自动把任意一级目录当作项目，避免将 `migrations`、`private` 或未来工具目录误识别为业务项目。

### 6.3 文件语义与轮转

- `runtime.log`：保存 INFO、WARNING、ERROR、CRITICAL，ERROR 会与 `error.log` 重复。保留重复是刻意设计，便于在同一时间线查看错误前后的上下文。
- `error.log`：只保存 ERROR、CRITICAL，异常记录必须附完整 traceback。
- 建议初始值：单文件 10 MiB，保留 10 个备份；UTF-8、追加模式、`delay=True`。
- 日志目录和所有轮转文件整体忽略，不只忽略当前的 `.log` 文件名。
- 如果明确要求 runtime 文件完全不含错误，可增加一个“最高 WARNING”filter；不推荐作为默认值，因为会破坏单文件时间线。

### 6.4 可读格式

建议文本格式：

```text
2026-09-07 10:26:31.482 | ERROR    | image_compression | pid=1234 MainThread | schedules.process_images:308 | 图片流水线失败 | mission_id=42 | source=album.zip
Traceback (most recent call last):
  ...完整调用栈...
```

至少包含：本地时间到毫秒、级别、项目、完整 logger 名或相对模块、进程 ID、线程名、源码行号、事件消息。路径与凭据不得作为默认字段输出。

日志消息使用参数化写法，避免在日志级别关闭时仍提前拼接：

```python
logger.info("任务处理完成 | mission_id=%s | file=%s", mission.id, safe_name)

try:
    run_pipeline()
except Exception:
    logger.exception("任务处理失败 | mission_id=%s | file=%s", mission.id, safe_name)
    raise
```

### 6.5 异常与数据库摘要的分工

- 数据库 `error_message`：继续保存脱敏、截断后的业务摘要，维持现有状态机和 API 行为。
- `error.log`：保存同一异常的完整 traceback、异常类型、调用文件与行号，供开发排查。
- 在异常被转换、吞掉或返回 `None` 之前记录；重新抛出的顶层异常也要先用子项目 logger 记录，确保进入对应目录。
- 对清理临时文件时现有的空 `except: pass`，若属于意外失败，应至少记录带 traceback 的 ERROR；不得因为新增日志改变原有回滚、重试、状态或重新抛出行为。
- 业务拒绝、同名冲突、无待处理任务等“预期失败”按 WARNING/INFO 记录，不伪造 traceback。

### 6.6 敏感信息与日志注入防护

1. 不记录数据库 URI、环境变量值、token、cookie、Authorization、完整 headers、简历全文或完整请求体。
2. `jd_auto_match/apis/score.py` 删除完整 `data` 输出；如确需诊断，只记录经过长度限制的职位 ID/标题等允许字段，默认使用 DEBUG。
3. `iwara/apis/log.py` 只接受白名单等级；对文本限制长度，将 CR/LF 等控制字符转义为单行，不记录浏览器请求头。
4. 外部工具 stderr 继续采用现有的容错解码和长度限制；完整 traceback 指 Python 调用栈，不等于无限保存 FFmpeg/7-Zip 输出。
5. 增加 `automation-server/**/logs/` 到 `.gitignore`，并用 `git check-ignore` 验证普通日志和轮转日志均被忽略。

### 6.7 并发边界

当前 `main.py` 以 `use_reloader=False` 启动单个 Python 进程，标准 `RotatingFileHandler` 足以支持同进程内 Flask/APScheduler 的多线程写入。

如果将来改为多个服务进程同时写相同文件，不能继续直接使用标准 FileHandler。Python 官方说明标准文件 handler 不提供跨进程串行化；届时有两条升级路径：

- 使用 `QueueHandler`，让所有进程把记录发送给单独的 QueueListener/日志进程统一落盘；
- 在保持标准 logging API 的前提下，引入并验证 `concurrent-log-handler` 等进程安全轮转 handler。

不建议在当前单进程基线上提前增加监听进程复杂度，但应为 `logging_config.py` 保留替换 handler 的边界。

## 7. 预计改动范围

| 路径 | 预计改动 | 对业务行为影响 |
| --- | --- | --- |
| `automation-server/logging_config.py` | 新增中央配置、格式、路由、轮转与幂等初始化 | 无业务行为变化 |
| `automation-server/app.py` | 在 Flask app 创建前调用一次日志配置 | 初始化顺序仅增加日志配置 |
| 现有 8 个含 `print()` 的 Python 文件 | 声明模块 logger，按语义替换 21 处输出 | 不改变路由、状态、事务与任务结果 |
| 图片/视频 scheduler 异常边界 | 在返回或重新抛出前写完整异常 | 保留原回滚和状态语义 |
| `.gitignore` | 忽略主服务及子项目 `logs/` | 防止运行数据进入 Git |
| 日志配置测试 | 覆盖分流、错误文件、堆栈、轮转、幂等和脱敏 | 只使用临时目录，不启动真实服务 |

初始实现不需要修改模型、迁移、API 响应、任务状态、文件命名、下载协议或各业务 service 的核心逻辑，也不需要引入 Loguru/structlog。

## 8. 建议实施阶段

### 阶段 1：日志基础设施

- 新增幂等的 `configure_logging()`；
- 创建主服务与子项目 handler；
- 配置 UTF-8、可读格式、双文件、轮转；
- 在 app 创建前初始化；
- 更新 `.gitignore`。

### 阶段 2：最小迁移

- 先迁移图片、视频 scheduler 的失败日志并保留完整堆栈；
- 再迁移 `iwara` 与 `jd_auto_match` 的正常/警告日志；
- 加固浏览器日志入口，移除完整请求数据输出；
- 顶层 scheduler 重新抛出行为保持不变。

### 阶段 3：验证与文档

- 使用临时日志根目录运行离线测试；
- 验证各子项目之间不串写、同一记录不重复；
- 验证 error 文件包含完整异常链和调用帧；
- 验证轮转、UTF-8 中文和重复初始化；
- 验证日志文件被 Git 忽略；
- 补充 README 中的日志目录、查看方式和级别说明。

## 9. 验收标准

1. 分别从五个顶层业务包发出 INFO 后，只进入各自 `runtime.log`，控制台行能直接看到项目名。
2. 从图片或视频任务制造一个离线异常后，对应 `error.log` 包含异常类型、消息、完整 traceback 和源码位置；数据库仍只保存既有长度限制的摘要。
3. ERROR 同时出现在同项目的 `runtime.log` 和 `error.log`，不进入其他子项目文件。
4. Flask/Werkzeug/APScheduler 日志进入主服务目录；业务任务主动记录的异常进入对应子项目目录。
5. 日志达到阈值后正确轮转，备份数量不超过配置；服务重启后继续追加。
6. 连续调用两次配置函数不会产生重复行或重复 handler。
7. 中文文件名和消息以 UTF-8 正确显示；超长浏览器消息被限制，换行不能伪造新日志记录。
8. 日志中不出现测试 token、cookie、Authorization、数据库连接串、完整简历或完整请求头。
9. 所有 `logs/`、`runtime.log.*`、`error.log.*` 均被 Git 忽略。
10. 相关 Python 文件编译通过；已有图片和视频单元测试不因日志改造而改变结果。

## 10. 最终建议

当前应直接采用方案 A，并把“结构化上下文”限制在轻量的 `mission_id`、安全文件名、状态和耗时字段。它能立刻解决控制台混杂、异常不可追踪和子项目难区分的问题，同时保持业务改动很小。

暂不引入 Loguru 或 structlog。等出现以下明确需求时再升级：

- 多进程同时写同一文件：先升级 QueueHandler/QueueListener 或并发轮转 handler；
- 接入集中日志平台、按字段聚合与跨任务链路追踪：在现有标准 logging 之上引入 structlog；
- 明确更重视本地终端体验并接受桥接维护：再评估 Loguru。

## 11. 官方资料

- [Python logging 文档：logger 层级、`exc_info` 与 `Logger.exception`](https://docs.python.org/3/library/logging.html)
- [Python logging handlers：RotatingFileHandler、TimedRotatingFileHandler、QueueHandler](https://docs.python.org/3/library/logging.handlers.html)
- [Python Logging Cookbook：filter、上下文与多进程单文件限制](https://docs.python.org/3/howto/logging-cookbook.html)
- [Flask Logging：尽早配置并在创建 app 前完成](https://flask.palletsprojects.com/en/stable/logging/)
- [Loguru 官方文档：sink、rotation、retention、enqueue 与安全注意事项](https://loguru.readthedocs.io/en/stable/api/logger.html)
- [structlog 标准 logging 集成：ProcessorFormatter 与配置取舍](https://www.structlog.org/en/stable/standard-library.html)

