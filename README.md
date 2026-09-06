# Automation Flow 项目

## 项目目标

本仓库用于实现仅在本地运行的自动化流水线。主服务位于 `automation-server/`，使用 Flask、Flask-SQLAlchemy 和 Flask-APScheduler；各条业务流水线是该目录下彼此独立的 Python 子包。
