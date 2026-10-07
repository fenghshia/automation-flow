# 本地运行工作台

## 入口与使用

使用 Flask Blueprint、Jinja 模板、本地 CSS 和少量原生 JavaScript，无需构建前端或安装依赖。重启现有主服务后访问：

- `/`：根路由未被其他服务占用时跳转至工作台。
- `/dashboard/`：已接入服务的入口导航，目前接入视频偏好筛选。
- `/video_filter/dashboard/`：视频筛选进度与模型表现。
- `/video_filter/dashboard/fragment`：自动刷新使用的服务端 HTML 片段。

主服务现有启动地址为 `http://127.0.0.1:6778/`。页面每 5 秒读取一次状态，可暂停、恢复和立即刷新；隐藏标签页暂停轮询。请求失败保留上次数据，并逐步延长重试间隔至 30 秒。页面请求不触发扫描、训练或搬运，刷新频率与业务调度间隔无关。

## 视频筛选显示内容

| 区域 | 含义 |
| --- | --- |
| 服务状态 | 已启用、分类采用的模型、自动分类门槛、配置与冲突提示 |
| 摘要覆盖 | 仍在受管目录中的编码版本数量、同当前特征签名的已提交摘要数量和比例 |
| 训练样本 | 当前签名下有完整摘要的确认喜欢/不喜欢资产数量；原视频已删除的负样本仍计入 |
| 当前任务 | 类型、文件名、任务 ID、耗时、当前阶段、窗口完成数或 MIL 训练轮次 |
| 模型比较 | 两类模型的活动版本、共同反馈样本上的实际准确率、不喜欢预测精确率、误判和 95% 区间 |
| 验证结果 | 活动模型验证集 balanced accuracy，单独显示，不当作实际用户反馈准确率 |
| 最近任务 | 最多 20 条，进行中的优先；包含状态、耗时、错误代码与创建时间 |
| 模型版本 | 最近 10 个版本各自的实际用户反馈成绩 |

实际指标沿用现有 `evaluation.actual_metrics()`：按资产去重、使用最新用户判断，排除该模型训练与验证快照中的样本；共同样本要求同一预测组的两类模型都有合格反馈。无反馈显示 `—`，不能解读为准确率为零。

窗口百分比仅表示 DINO、VideoMAE、BEATs 或 eGeMAPS 中的当前提取阶段；整部视频提交摘要后才计入覆盖率。MIL 可提前停止，轮次数表示当前轮次与最大轮数。逻辑回归求解器没有可用的逐迭代进度，仅显示阶段与耗时；不生成估计百分比。页面只展示任务实际报告的进度。

页面使用文件名和技术 ID，不显示完整目录路径、数值特征或模型参数；模板自动转义文件名。数据库不可用或迁移缺失显示可刷新提示，完整错误堆栈仍由既有日志系统记录到 `video_filter/logs/error.log`。

## 模块划分与后续服务接入

- `automation-server/dashboard/`：工作台导航、公共模板、静态资源及服务注册表，不读取业务数据。
- `automation-server/video_filter/dashboard.py` 与包内 `templates/`：视频筛选页面和只读路由。
- `video_filter/reporting.py`：共享状态查询供现有 JSON 状态接口与页面使用；只选统计和元数据，不加载特征/模型二进制。
- `video_filter/progress.py`：通过已有日志事件和 worker 通道同步实时进度，不改变任务状态机。

未来服务在自己的包中定义 Blueprint、统计查询和模板，模板继承 `dashboard/base.html`，在已有显式注册函数中注册 Blueprint，再调用公共入口：

```python
from dashboard import register_service

def register_example_dashboard(app, blueprint):
    if blueprint.name not in app.blueprints:
        app.register_blueprint(blueprint)
    register_service(
        app,
        key="example",
        title="示例服务",
        description="服务自己的进度说明。",
        endpoint="example_dashboard.index",
    )
```

注册在首次请求前完成，相同注册可重复调用；同一个 key 的冲突注册会报错。现有视频筛选的注册链已接通，工作台注册不会启动调度器。新增服务不需要让工作台导入其内部模块，也不需要引用视频筛选的数据模型。

## 存储与验证

资产、摘要、任务、模型和反馈继续使用现有数据库。本次没有新增表或迁移。

正在执行的任务将少量阶段元数据原子写入 `VIDEO_FILTER_STATE_DIR/progress/<task_uuid>.json`，用于跨进程显示窗口或轮次进度。这是本地显示缓存，不是训练数据；终态以数据库为准，结束任务不会再读取其缓存。每个记录只含经过白名单筛选的阶段、数字与时间，不保存媒体、文件路径或原始日志。缓存不可用时任务继续执行，页面显示未知进度；该目录内已结束任务的缓存可以在服务停止后按需清理。

验证采用隔离 Flask、临时 SQLite 和临时状态目录：105 项筛选测试通过，其中新增 10 项覆盖工作台注册、未来服务接入、旧根路由兼容、页面只读查询、文件名转义、配置/数据库故障提示、摘要签名变化和 worker 实时进度传输。浏览器验证桌面和手机布局、自动刷新、暂停恢复、手动刷新及断连保留数据。未连接生产数据库、启动真实调度器或处理真实视频。

```text
# 从 automation-server/ 执行
mamba run -n autoflow python -m unittest discover -s video_filter/tests -t .
mamba run -n autoflow python -m compileall -q dashboard video_filter
```
