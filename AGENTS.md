# Automation Flow 项目指令

## 项目目标

本仓库用于实现仅在本地运行的自动化流水线。主服务位于 `automation-server/`，使用 Flask、Flask-SQLAlchemy 和 Flask-APScheduler；各条业务流水线是该目录下彼此独立的 Python 子包。

正确性、可恢复性和数据安全优先于大范围重构。除非用户明确要求，不改变现有业务流程、外部服务协议或任务状态语义。

## 指令加载

根目录 `AGENTS.md` 是唯一自动发现入口，专项规则集中在 `.agents/`。开始修改前，根据目标路径和任务类型读取下表；同时命中时全部读取。普通 Markdown 链接不代表已加载，必须实际打开对应文件。

| 触发范围 | 必读文件 |
| --- | --- |
| `automation-server/**` | `.agents/automation-server.md` |
| `automation-server/<project_name>/**` | `.agents/subproject.md` |
| `automation-server/<project_name>/browser-plugin/**` | `.agents/firefox-browser-plugin.md` |
| `automation-server/<project_name>/tampermonkey/**` | `.agents/tampermonkey.md` |
| 配置、凭据、日志、样例数据、Git 初始化、提交或 GitHub 发布 | `.agents/public-repository-safety.md` |

专项规则补充或收紧本文件；冲突时，更具体的目标路径规则优先。仅当某个子项目存在无法由统一规则覆盖的额外技术约束时，才在 `.agents/` 新建职责明确的专项文件并同步补充本表；不要再把 `AGENTS.md` 散落到业务目录。

## 工作方式

1. 先检查目标文件、相邻实现、调用方和生效范围；不要只根据目录名猜测。
2. 只修改完成请求所需的文件，保留用户的既有改动，不顺手重构无关代码。
3. 信息轻微缺失时沿用同类项目的现有模式；只有不同选择会明显改变行为、数据结构或外部协议时才询问。
4. 新增一条流水线时，将代码放在 `automation-server/<project_name>/` 下，并由该包显式暴露需要注册的 API、模型和定时任务。
5. 按触发方式和代码归属放置代码：HTTP 接口放 `apis/`，数据库模型放 `models/`，调度入口放 `schedules/`，Firefox 扩展放 `browser-plugin/`，用户脚本放 `tampermonkey/`。被多个入口复用的业务逻辑放在该小项目包内的普通模块中，不要塞进某个入口目录。
6. 不要仅为统一风格而搬动现有模块；若移动会改变导入或注册副作用，必须同步检查入口。

## 本地环境与命令

- Python Conda 环境名固定为 `autoflow`。默认使用 `mamba run -n autoflow <command>`；若 `mamba` 不可用，再使用 `conda run -n autoflow <command>`。
- 不假设环境已激活，不把依赖安装到 base 或系统 Python。
- 命令默认从仓库根目录执行；需要读取 `alembic.ini` 的命令应在 `automation-server/` 下执行或显式指定配置路径。
- 不擅自安装、升级依赖或修改 Conda 配置。确需新增依赖时，先说明原因，并同步维护仓库实际采用的依赖清单；当前不存在依赖清单时不要虚构一个已生效的锁定流程。
- 不启动会持续占用终端的 Flask 服务或调度器来代替自动验证。需要人工联调时，给出精确命令和观察点。

## 验证与完成条件

- 验证应与改动风险匹配：先做最小的语法或静态检查，再运行仓库已有的相关测试；不要声称运行了不存在或未执行的测试。
- Python 语法检查可使用 `mamba run -n autoflow python -m compileall automation-server`。导入应用可能启动调度器或访问数据库，除非已确认隔离条件，否则不要把导入作为无副作用检查。
- 浏览器扩展或用户脚本按“指令加载”表读取专项验证规则。
- 若验证依赖本地数据库、Firefox、外部下载器、私有网络或真实数据且当前不可用，完成可执行的离线检查后明确报告未验证部分和人工复现方法。
- 完成时简要列出修改文件、行为变化、已执行验证及剩余风险。

## 权限边界

- 本项目只在本地运行，默认不为 Flask 接口新增认证、授权、CSRF 或公网部署方案；输入校验、错误处理和避免泄露敏感信息仍然需要。
- 未经明确要求，不初始化 Git、不提交、不推送、不执行数据库迁移、不清空数据、不运行真实定时任务，也不调用会产生下载、消息或其他外部副作用的接口。
- 发现疑似秘密或个人数据时不要在回复、补丁说明、日志或测试夹具中复述其原值；读取 `.agents/public-repository-safety.md` 后处理。
