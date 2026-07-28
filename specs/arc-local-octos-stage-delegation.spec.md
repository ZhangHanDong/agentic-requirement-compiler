spec: task
name: "ARC Local Octos Stage Delegation"
tags: [arc, octos, mcp, stdio, delegation]
estimate: 2h
---

## 意图

让 ARC 保留需求编译的流程编排、阶段顺序和系统侧验证，同时把
`InterfaceDesigner`、`TestGenerator`、`TestDrivenDeveloper` 三个模型执行阶段
交给本机 Octos 完成。用户只需指定本地 `octos` 可执行文件，不需要启动
`octos-tui`、HTTP 服务或配置 MCP bearer token。

## 已定决策

- 新增 `--octos-local` 启用本地执行，新增 `--octos-bin <PATH>` 指定 Octos；
  `ARC_OCTOS_LOCAL` 和 `OCTOS_BIN` 提供环境变量回退。
- ARC 使用 `asyncio.create_subprocess_exec` 启动
  `octos mcp-serve --transport stdio --cwd <ARC-output-dir>`，禁止经过 shell。
- 一次 `arc compile` 只启动一个 Octos 子进程，三个阶段通过同一 MCP 会话串行调用
  `run_octos_session`。
- stdio 客户端必须完成 `initialize`、`notifications/initialized` 和 `tools/list`
  后才允许调用工具，并验证服务器确实提供 `run_octos_session`。
- 阶段请求负载继续使用
  `{contract, input:{prompt, expected_artifact, artifact_name}}`，输出继续以
  `artifact_content` 作为 ARC 阶段结果。
- ARC 仍然拥有编译编排与测试验证；Octos 只替代阶段中的模型执行。
- 不增加第三方 Python 依赖，使用 Python 3.11 标准库实现 stdio JSON-RPC 生命周期。

## 边界

### 允许修改
- src/integrations/octos_mcp.py
- src/main.py
- tests/test_octos_mcp.py
- AGENT-CHAT.md
- specs/arc-local-octos-stage-delegation.spec.md

### 禁止做
- 不修改 Octos 或 octos-tui 仓库。
- 不让本地模式依赖 HTTP 端口、bearer token 或 `octos-tui`。
- 不通过 shell 字符串启动 Octos。
- 不移除或改变现有 `--octos-mcp` HTTP 配置的对外含义。
- 不让 ARC 在本地 Octos 模式下回退到自己的 LLM。

## 完成条件

### 规则: local-cli-selection — 本地 Octos 是显式且互斥的执行后端

场景: compile 命令选择本地 Octos
  测试:
    过滤: test_local_octos_cli_builds_stdio_delegator_for_output_workspace
    层级: integration
    替身: temporary_executable
    命中: src/main.py
  假设 `octos` 可执行文件存在
  当 用户执行 `arc compile ... --octos-local --octos-bin <PATH>`
  那么 ARC 创建绑定到输出目录的本地 stdio delegator
  并且 delegator 的启动参数包含 `mcp-serve --transport stdio --cwd <output-dir>`

场景: 本地 Octos 与其他委托后端互斥
  测试: test_local_octos_cli_rejects_multiple_delegation_backends
  假设 用户已经配置 `--octos-local`
  当 用户同时配置 `--octos-mcp` 或 `--delegate-to`
  那么 ARC 在启动编译前返回互斥配置错误

场景: 本地 Octos 可执行文件不存在
  测试:
    过滤: test_local_octos_cli_rejects_missing_binary
    层级: integration
    替身: missing_filesystem_path
    命中: src/main.py
  假设 `--octos-bin` 指向不存在的文件
  当 ARC 解析本地 Octos 后端
  那么 ARC 在启动编译前返回包含该路径的配置错误

### 规则: stdio-mcp-lifecycle — ARC 遵循 MCP stdio 生命周期

场景: 本地 Octos 完成阶段并复用同一进程
  测试: test_local_octos_stdio_delegates_stage_and_reuses_process
  假设 本地 MCP 服务支持 `run_octos_session`
  当 ARC 连续委托两个编译阶段
  那么 ARC 只启动一个 Octos 子进程
  并且按顺序完成 `initialize`、`notifications/initialized`、`tools/list` 和两次 `tools/call`
  并且阶段请求负载继续使用 `{contract, input:{prompt, expected_artifact, artifact_name}}`
  并且两个阶段都从 `artifact_content` 得到结果

场景: 本地 MCP 服务未提供 Octos 工具
  测试: test_local_octos_stdio_rejects_server_without_run_session_tool
  假设 MCP 初始化成功但 `tools/list` 不包含 `run_octos_session`
  当 ARC 准备委托第一个编译阶段
  那么 ARC 返回包含缺失工具名称的阶段委托错误
  并且不发送 `tools/call`

场景: Octos 返回工具级失败
  测试: test_local_octos_stdio_surfaces_tool_error
  假设 `run_octos_session` 返回 `isError` 和 typed error
  当 ARC 等待阶段结果
  那么 ARC 返回包含 Octos typed error 的阶段委托错误
  并且不把该阶段标记为成功

场景: Octos 在超时时间内没有响应
  测试:
    过滤: test_local_octos_stdio_timeout_stops_child_process
    层级: integration
    替身: local_stdio_server
    命中: src/integrations/octos_mcp.py
  假设 本地 Octos 已启动但不返回 MCP 响应
  当 请求超过配置的 timeout
  那么 ARC 返回明确的超时错误
  并且终止该 Octos 子进程以避免响应错位

场景: ARC 编译结束后关闭本地 Octos
  测试: test_local_octos_stdio_close_ends_child_process
  假设 ARC 已经建立本地 Octos MCP 会话
  当 compile 的 finally 清理 delegator
  那么 ARC 关闭子进程 stdin 并等待进程退出
  并且进程未退出时执行有界 terminate 或 kill

## 排除范围

- 修复或重写远程 MCP Streamable HTTP 客户端。
- 把整个 ARC 编译器作为一个 Octos session 执行。
- 修改 Octos provider、profile、sandbox 或 workspace contract。
- 集成 `octos-tui`。
- 为 agent-chat `serve` 常驻 worker 增加多 workspace 的本地 Octos 进程池。
