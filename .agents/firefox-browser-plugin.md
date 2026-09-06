# Firefox 扩展规则

仅适用于 `automation-server/<project_name>/browser-plugin/**`。同时读取 `.agents/automation-server.md` 与 `.agents/subproject.md`。

- 本目录仅支持 Firefox；除非用户明确扩大范围，不增加 Chrome、Edge 或跨浏览器兼容层。
- `code/` 是可编辑源代码；`dist/` 中的 `.xpi` 是生成产物。修改源代码后不要手工编辑已有 `.xpi`，也不要默认提交新的打包产物。
- 保持 Firefox 支持的 manifest 语义以及 `browser_specific_settings.gecko` 配置。扩展 id、版本号和最低 Firefox 版本只在发布或兼容性需求明确时修改。
- 权限、host permissions、内容脚本匹配范围和页面注入时机遵循最小范围；新增权限必须与具体功能对应。
- 后台脚本、内容脚本与本地 Flask API 的消息格式必须一致。网络失败、服务未启动或页面结构不匹配时应安全失败，不持续刷请求。
- 不把 token、cookie、真实请求头、个人数据或本机专属配置写入 manifest、源码或构建脚本。
- 若已安装 `web-ext`，从目标 `browser-plugin/` 目录运行 `web-ext lint --source-dir code`。只有用户要求构建时才运行打包流程；构建后报告生成文件，不覆盖不相关版本。
