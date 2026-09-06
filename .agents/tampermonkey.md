# Tampermonkey 用户脚本规则

仅适用于 `automation-server/<project_name>/tampermonkey/**`。同时读取 `.agents/automation-server.md` 与 `.agents/subproject.md`。

- 每个用户脚本应保持完整且可独立安装，保留有效的 `// ==UserScript==` 元数据块。
- `@match`、`@include`、`@connect` 和 `@grant` 只声明功能需要的最小范围；使用的 Tampermonkey API 必须有对应 grant。
- 与本地 Flask API 交互时复用当前路由和数据契约。接口不可用、响应无效或页面 DOM 变化时应给出有限、可诊断的失败，不无限轮询。
- 不在脚本中固化秘密、cookie、真实身份数据或机器专属路径。需要配置的值集中放在清晰的配置区，并提供无敏感信息的默认值。
- 修改 DOM 时限定目标节点，避免重复插入，并在 SPA 导航或脚本重复执行时保持幂等。
- 完成后至少做 JavaScript 语法检查和元数据或权限人工核对；需要真实站点或 Tampermonkey 的行为应明确标为人工验证。
