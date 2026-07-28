spec: task
name: "ARC Replaceable Agent Backend"
tags: [arc, agent-backend, octos, compiled-task, delegation]
estimate: 4h
---

## 意图

把 ARC 拆成需求编译前端与可替换的 agent 执行后端。三个阶段 adapter 继续负责
构造上下文、prompt、输出 schema 和系统验收条件，但不再直接创建 deep-agent
或读取全局 `StageDelegator`。它们统一生成版本化的 `arc.agent-task.v1` 执行包，
再交给 `BuiltinAgentBackend` 或 `LocalOctosBackend` 执行。

本任务是“替换 ARC 当前 agent backend”的第一版：执行单位仍是 ARC 编排的阶段
任务，而不是把整个 ARC 编译器塞进一个 Octos session。ARC 继续拥有需求树、
依赖排序、阶段状态、测试验证、checkpoint、重试和 traceability。

## 已定决策

- 新增 `AgentBackend` 抽象与 `CompiledAgentTask`；所有 backend 消费同一任务对象。
- 执行包 schema 固定为 `arc.agent-task.v1`，持久化到
  `<workspace>/.arc/agent_tasks/`，只包含 JSON 可序列化的编译输入，不包含 Python
  callable、模型对象或其他进程内运行时对象。
- `BuiltinAgentBackend` 封装现有 `build_stage_agent` +
  `ainvoke_stage_agent` 路径；现有内置行为保持默认。
- `LocalOctosBackend` 封装本地 Octos stdio delegator，消费同一个执行包，并把
  system prompt、task prompt 和 response schema 映射到 `run_octos_session`。
- 外部 backend 完成 TDD 后，ARC 必须继续亲自执行当前节点测试；backend 的成功
  声明不能替代系统验证。
- `ARCWorkflowManager` 通过构造参数注入一个 backend，并把同一实例交给三个阶段
  adapter；compile 生命周期只关闭一次。
- 新增 `--agent-backend {builtin,octos-local}` 与 `ARC_AGENT_BACKEND`；现有
  `--octos-local`、`--octos-bin`、`--octos-mcp`、`--delegate-to` 保持兼容。
- 保留旧 `set_stage_delegator`/`get_stage_delegator` API 供兼容调用，但新的三个
  adapter 和 CLI workflow 不再依赖该全局 seam。

## 边界

### 允许修改

- `src/agents/backend.py`
- `src/agents/interface_designer.py`
- `src/agents/test_generator.py`
- `src/agents/test_driven_developer.py`
- `src/core/workflow.py`
- `src/main.py`
- `src/integrations/octos_mcp.py`
- `src/integrations/stage_delegation.py`
- `tests/test_agent_backend.py`
- `tests/test_delegated_adapters.py`
- `tests/test_octos_mcp.py`
- `AGENT-CHAT.md`
- `OCTOS.md`
- `README.md`
- `specs/arc-replaceable-agent-backend.spec.md`

### 禁止做

- 不修改 Octos 或 octos-tui 仓库。
- 不改变 ARC 的需求树遍历、DESIGN/IMPLEMENT 顺序或 checkpoint 语义。
- 不让 backend 绕过 ARC 的 TDD 系统测试验证。
- 不把 Python callable、API key 或模型对象写入执行包。
- 不移除现有 agent-chat 或远程 Octos 兼容入口。
- 不让本地 Octos 模式回退到 ARC 自有模型。

## 完成条件

### 规则: compiled-agent-task — ARC 产生稳定、可审计的 backend 输入

场景: 执行包可安全序列化并持久化
  测试: test_compiled_agent_task_persists_versioned_json_without_runtime_objects
  假设 adapter 已编译一个包含运行时 model、response format 和 callable tools 的任务
  当 backend 开始执行该任务
  那么 `.arc/agent_tasks` 中保存 `arc.agent-task.v1` JSON
  并且 JSON 包含 task id、stage、node、phase、prompt、inputs、acceptance 和 schema
  并且 JSON 不包含 callable、API key 或 Python model 对象

场景: 相同任务可被内置 backend 执行
  测试: test_builtin_agent_backend_executes_compiled_task_with_runtime_context
  假设 `CompiledAgentTask` 带有内置模型、response format、skills 和 runtime tools
  当 `BuiltinAgentBackend` 执行任务
  那么它通过现有 deep-agent runner 返回结构化输出
  并且传入正确的 workspace、thread、context 和 runtime tools

### 规则: backend-selection — workflow 使用显式且可替换的 backend

场景: workflow 向三个 adapter 注入同一个 backend
  测试: test_workflow_injects_one_backend_into_all_stage_adapters
  假设调用方构造 `ARCWorkflowManager(agent_backend=backend)`
  当 workflow 初始化三个阶段 adapter
  那么三个 adapter 都持有同一个 backend 实例
  并且 adapter 不读取全局 `StageDelegator`

场景: compile 显式选择本地 Octos backend
  测试:
    过滤: test_agent_backend_cli_selects_local_octos_and_preserves_legacy_aliases
    层级: integration
    替身: temporary_executable
    命中: src/main.py, src/agents/backend.py
  假设 Octos 可执行文件存在
  当用户传入 `--agent-backend octos-local` 或原有 `--octos-local`
  那么 ARC 构造 `LocalOctosBackend`
  并且本地 stdio delegator 仍绑定 ARC 输出目录

场景: compile 显式选择内置 backend
  测试: test_agent_backend_cli_rejects_builtin_with_delegation_flags
  假设用户传入 `--agent-backend builtin`
  当命令同时包含 Octos 或 agent-chat 阶段委托配置
  那么 ARC 在开始编译前返回配置冲突

### 规则: adapter-migration — 三个阶段统一提交编译任务

场景: 设计与测试 adapter 生成结构化 backend 任务
  测试: test_design_adapters_submit_compiled_tasks_to_backend
  假设注入记录型 backend
  当运行 InterfaceDesigner 和 TestGenerator
  那么 backend 收到各自的 `CompiledAgentTask`
  并且 inputs 保留 requirement、interface contract 和 response schema

场景: TDD adapter 把实现任务交给外部 backend 后独立验收
  测试: test_external_backend_tdd_claim_requires_system_test_pass
  假设外部 backend 返回实现成功
  当 ARC 的系统测试失败
  那么 adapter 不返回 IMPLEMENTED
  并且失败输出包含系统测试结果

### 规则: octos-backend — Octos 消费统一任务而不是 adapter 特判

场景: LocalOctosBackend 映射统一任务到 Octos
  测试: test_local_octos_backend_executes_same_compiled_task_contract
  假设本地 Octos delegator 可用
  当 `LocalOctosBackend` 执行 `CompiledAgentTask`
  那么它调用 delegator 的 `invoke_stage`
  并且 stage、node、phase、workspace、prompts 和 response schema 与执行包一致

场景: backend 生命周期只关闭一次
  测试: test_compile_closes_injected_agent_backend_once
  假设 compile 使用外部 backend
  当编译正常结束或失败
  那么 ARC 清理同一个 backend 一次
  并且 backend 负责关闭其底层 Octos 或 agent-chat transport

## 排除范围

- 单一 Octos session 执行整棵需求树。
- Octos 内部阶段流式事件转译为 ARC 进度事件。
- 多 workspace 常驻 Octos 进程池。
- 远程 MCP Streamable HTTP 协议升级。
- 删除旧 `StageDelegator` API。
