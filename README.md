# Automation Flow 项目

## 项目目标

本仓库用于实现仅在本地运行的自动化流水线。主服务位于 `automation-server/`，使用 Flask、Flask-SQLAlchemy 和 Flask-APScheduler；各条业务流水线是该目录下彼此独立的 Python 子包。

## 日志与排障

服务启动时会创建按大小轮转的 UTF-8 日志文件（单文件 10 MiB，保留 10 个备份）：

- 框架日志：`automation-server/logs/runtime.log` 与 `error.log`；
- 子项目日志：`automation-server/<子项目>/logs/runtime.log` 与 `error.log`。

`runtime.log` 保存 INFO 及以上的完整时间线，`error.log` 只保存 ERROR/CRITICAL。子项目异常只写入该子项目目录，不会写入框架错误日志；Python 异常记录包含完整 traceback。日志目录不可创建或文件不可打开时，服务会拒绝启动，避免在无持久日志的状态下运行。

排查问题时先查看对应子项目的 `error.log`，再结合该目录的 `runtime.log` 查看异常前后的任务状态。日志文件和轮转备份均已加入 `.gitignore`，不得提交到仓库。
