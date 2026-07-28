# 使用 Octos 执行 ARC 需求编译

ARC 可以保留需求解析、任务编排、测试验证和可追溯性管理，同时把
`InterfaceDesigner`、`TestGenerator`、`TestDrivenDeveloper` 三个模型执行阶段
交给本机 Octos。推荐使用本地 stdio MCP 模式：ARC 会自行启动和关闭 Octos，
不需要运行 `octos-tui`，也不需要配置 HTTP 端口或 MCP bearer token。

> **当前状态**：ARC 已实现可替换的 agent backend。三个阶段 adapter 负责把
> 需求编译成 `arc.agent-task.v1` 执行包，内置 backend 与本地 Octos backend
> 消费同一个 contract。执行单位仍是 ARC 编排的阶段任务，不是用一个 Octos
> session 执行整棵需求树。

下面的命令以 macOS/Linux shell 为例；Windows 用户需要使用对应的 PowerShell
环境激活、命令查找和环境变量语法。

## 1. 前置条件

### 安装 ARC

ARC 要求 Python 3.11 或更高版本。在 ARC 仓库中安装开发版本：

```bash
uv venv
source .venv/bin/activate
uv pip install -e .

arc --version
```

还需安装目标应用类型使用的工具链：

| 应用类型 | ARC 检查的命令 | 其他要求 |
| --- | --- | --- |
| `web` | `node`、`npm` | Node.js 20+ |
| `android` | `java` | JDK 21、Android SDK |
| `cli` | `python` | Python 3.11+ |

### 安装并配置 Octos

可以通过 Homebrew 安装 Octos：

```bash
brew tap octos-org/octos https://github.com/octos-org/octos
brew install octos-org/octos/octos
```

也可以通过 npm 安装：

```bash
npm install -g @octos-org/octos
```

可选：从本地源码安装需要 Rust 和 Cargo。下面是 Octos 官方的完整功能构建；
ARC 本身只使用其中的 `mcp-serve`：

```bash
cd /path/to/octos
cargo install --path crates/octos-cli \
  --features "api,telegram,discord,dingtalk,whatsapp,feishu,twilio,wecom,wecom-bot"
```

首次使用时，为 Octos 选择模型供应商和模型，并配置对应凭据：

```bash
octos init
```

`octos init` 可以在交互过程中保存 API key。如果当时跳过了凭据配置，可随后
登录所选供应商，例如：

```bash
octos auth login --provider deepseek
```

也可以按 Octos 的供应商约定通过环境变量提供 API key。

最后确认 ARC 将要调用的命令存在：

```bash
command -v octos
octos --version
octos mcp-serve --help
```

建议先运行一次 `octos chat`，确认所选模型、凭据和网络均可正常工作。ARC
委托给 Octos 的模型调用会产生相应供应商的 token 用量和费用；节点数、测试
层级和失败重试都会增加调用次数。

当前集成已使用 Octos 2.0.2 验证 stdio MCP 初始化、工具发现和进程清理。实际
生成质量和完整编译结果仍取决于所选供应商与模型；执行真实编译前请确认可接受
相应模型费用。

## 2. 使用本地 Octos 编译

需求目录必须包含 `requirements.yaml` 或 `requirements.yml`，也可以直接传入
其中一个 YAML 文件。最小目录结构如下：

```text
my-requirements/
└── requirements.yaml
```

输出目录是 ARC 将创建或继续使用的工作区。如果 `octos` 已在 `PATH` 中，在
ARC 仓库中执行：

```bash
arc compile /path/to/my-requirements \
  -o /path/to/output-workspace \
  --agent-backend octos-local
```

例如：

```bash
arc compile example/ticketbooking-demo \
  -o workspace/ticketbooking-octos \
  --type web \
  --agent-backend octos-local
```

如果 Octos 不在 `PATH` 中，通过绝对路径指定二进制即可：

```bash
arc compile example/ticketbooking-demo \
  -o workspace/ticketbooking-octos \
  --agent-backend octos-local \
  --octos-bin /absolute/path/to/octos
```

原有 `--octos-local` 仍是 `--agent-backend octos-local` 的兼容入口；
`--octos-bin` 本身也会启用本地 Octos。

编译期间，ARC 会启动等价于下面的子进程：

```bash
octos mcp-serve --transport stdio --cwd /path/to/output-workspace
```

正常情况下，一次 `arc compile` 会跨阶段复用同一个 Octos 子进程。传输失败或
超时后 ARC 会先终止失效进程，后续阶段如仍可执行，可能启动新进程。ARC 会完成
MCP 初始化和工具发现，然后按顺序调用 Octos 的 `run_octos_session` 工具；编译
结束、失败、超时或取消时都会清理当前子进程。

## 3. 使用环境变量

如果不希望每次传递命令行参数，可以设置：

```bash
export ARC_AGENT_BACKEND=octos-local
export OCTOS_BIN="$(command -v octos)"

arc compile /path/to/my-requirements -o /path/to/output-workspace
```

可用配置如下：

| 环境变量 | 默认值 | 作用 |
| --- | --- | --- |
| `ARC_AGENT_BACKEND` | `builtin` | 显式选择 `builtin` 或 `octos-local` |
| `ARC_OCTOS_LOCAL` | 未启用 | 设为 `1`、`true`、`yes` 或 `on` 时启用本地 Octos |
| `OCTOS_BIN` | `octos` | Octos 可执行文件的路径或 `PATH` 中的命令名 |
| `OCTOS_MCP_CONTRACT` | `coding` | 传给 `run_octos_session` 的 Octos contract |
| `OCTOS_MCP_TIMEOUT` | `1800` | 单个编译阶段的超时时间，单位为秒 |
| `OCTOS_MCP_STARTUP_TIMEOUT` | `30` | 每个 MCP 启动请求的超时时间，单位为秒 |

推荐设置 `ARC_AGENT_BACKEND=octos-local` 和 `OCTOS_BIN`。
`ARC_OCTOS_LOCAL=1` 是旧配置的兼容方式。除非自定义了 Octos contract，否则
应保留 `OCTOS_MCP_CONTRACT=coding`。

Octos 会优先读取输出工作区中的 `.octos/config.json`，再使用全局配置。如果
同一个输出目录曾保存项目级 Octos 配置，应检查其中的 provider、model、
sandbox 和 tool policy 是否符合本次编译预期。

## 4. ARC 和 Octos 各自负责什么

| 组件 | 职责 |
| --- | --- |
| ARC | 读取结构化需求、安排节点和阶段、维护编译状态、保存可追溯记录、运行生成的测试 |
| Octos | 使用自身配置的模型和工具执行接口设计、测试生成和测试驱动开发阶段 |

每次执行前，ARC 会把编译后的 backend 输入保存到
`<output-dir>/.arc/agent_tasks/`。本地 Octos backend 把同一执行包交给
`run_octos_session`，Octos 在输出目录中工作，并把阶段结果写入
`<output-dir>/.arc/delegated/`。对于 `TestDrivenDeveloper` 阶段，Octos 返回
完成状态后，ARC 仍会亲自运行该节点的测试；只有测试进程成功退出，ARC 才接受
`IMPLEMENTED`。

本地 Octos 模式替代的是上述三个阶段的模型调用，因此 ARC 不需要为这些调用
单独配置 OpenAI-compatible API key。如果需求包含截图，截图分析仍由 ARC
自己的视觉模型配置处理。可以通过 `VISUAL_API_KEY`、`VISUAL_BASE_URL` 和
`VISUAL_MODEL` 独立配置；未设置时会分别回退到 `OPENAI_API_KEY`、
`OPENAI_BASE_URL`/`OPENAI_API_BASE` 和 `MODEL`。

当前 `arc doctor` 仍按 ARC 自有模型模式检查 `OPENAI_API_KEY`、
`OPENAI_BASE_URL`、`MODEL` 和 `ARC_OPENAI_API_MODE`。无截图的本地 Octos
编译并不使用前三项，因此这些 doctor 错误是当前校验器尚未感知委托模式的已知
限制，不代表 Octos MCP 无法启动。

## 5. 恢复失败的编译

恢复时仍需启用同一个 Octos 后端：

```bash
arc compile /path/to/my-requirements \
  -o /path/to/existing-output-workspace \
  --resume \
  --retry-failed \
  --agent-backend octos-local
```

也可以使用 `--retry <NODE_ID...>` 只重试指定需求节点。

## 6. 限制

- 本地 Octos 目前只支持 `arc compile`，不支持 `arc serve` 的常驻 worker。
- 本地 Octos（stdio MCP，推荐）、旧式远程 HTTP MCP（`--octos-mcp`）和
  agent-chat 阶段委托（`--delegate-to`）三个已配置的阶段执行后端互斥。
- 当前 Octos 的远程传输使用带会话和 SSE 语义的 MCP Streamable HTTP，ARC
  保留的旧式远程 HTTP MCP 客户端尚不兼容；与当前 Octos 直接集成时应使用
  本文的本地 stdio 模式。
- `octos-tui` 是交互式终端客户端，不参与 ARC 到 Octos 的委托链路。
- `run_octos_session` 只在阶段结束后返回，不会把 Octos 内部工具活动实时流式
  回传给 ARC。长阶段可能暂时没有新输出，可查看
  `<output-dir>/.arc/debug.log` 中的 ARC 日志。

## 7. Agent backend 架构与后续路线

ARC 当前已经拆成“编译前端”和“agent 执行后端”：

```text
结构化需求
    │
    ▼
ARC 编译前端
  - 需求树与依赖
  - 执行任务与上下文
  - 交付物和验收条件
  - trace / resume 标识
    │
    ▼
Agent Backend
  ├── ARC 内置 backend
  └── 本地 Octos backend
           │
           ▼
      代码、测试、结构化结果
```

ARC 先把需求节点编译成稳定的执行包，再交给选定 backend。Octos
不再只是分别代跑 `InterfaceDesigner`、`TestGenerator` 和
`TestDrivenDeveloper` 的 prompt，而是直接消费编译后的任务，负责完整的 agent
loop、工具调用和工作区修改。

ARC 仍保留编译器应拥有的系统职责：

- 需求解析、依赖排序和任务调度；
- 执行包 schema、交付物 contract 和 traceability ID；
- 测试与验收条件的独立验证；
- checkpoint、失败恢复、重试和最终编译状态。

已经完成：

1. 统一 `AgentBackend` 协议，三个 adapter 不再读取全局 `StageDelegator`；
2. 版本化的 `arc.agent-task.v1` 执行包，包含节点、上下文、交付物与验收条件；
3. `BuiltinAgentBackend` 和 `LocalOctosBackend` 消费相同任务对象；
4. workflow 向三个 adapter 注入同一个 backend，并统一管理生命周期；
5. 外部 backend 的 TDD 成功声明继续接受 ARC 系统测试验证。

后续仍计划：

- 使用真实付费模型完成内置与 Octos backend 的端到端效果一致性测试；
- 把 Octos 内部阶段事件流式转译为 ARC 进度；
- 升级远程 Octos transport 到 MCP Streamable HTTP；
- 为 `arc serve` 增加多 workspace 的本地 Octos 进程池。

## 8. 常见问题

### 找不到 Octos 或文件不可执行

如果 ARC 报告 `local octos binary was not found on PATH` 或
`local octos binary is missing or not executable`，先检查：

```bash
command -v octos
ls -l "$(command -v octos)"
```

然后通过 `--octos-bin` 或 `OCTOS_BIN` 传入真实的可执行文件路径。这里需要的是
编译后的 `octos` 二进制，不是 Octos 源码目录。

### Octos 未提供 `run_octos_session`

`local octos MCP server does not expose required tool 'run_octos_session'`
表示当前二进制不具备 ARC 所需的 MCP 子代理工具。升级 Octos，或从包含
`mcp-serve` 的当前源码重新编译，再重新运行 `arc compile`。ARC 会在首次阶段
调用前通过 MCP 工具发现验证 `run_octos_session`。

### `config_error`、`llm_error` 或供应商认证失败

先直接运行：

```bash
octos chat
```

如果 Octos 自身也无法调用模型，重新执行 `octos init`，并使用
`octos auth login --provider deepseek`（替换为实际供应商）或供应商 API key
环境变量修复凭据。

如果 Octos 报告 `open episode store`，通常是另一个 `octos serve`、
`octos-tui` 后端或 Octos 会话占用了相同的默认数据目录。使用 `octos status`
检查正在运行的实例，并在确认不再需要后先停止对应进程，再恢复 ARC 编译。

### 没有可用的 sandbox backend

Octos 的 `mcp-serve` 默认采用 fail-closed 策略：配置要求 sandbox、但当前系统
没有可用后端时，它会拒绝运行工具。优先安装或修复 Octos 支持的系统 sandbox。
如果明确接受无隔离执行的风险，也可以在实际生效的 Octos 配置中设置
`sandbox.mode = "none"`；这会降低安全性，不应作为默认方案。

### 阶段或启动超时

模型执行时间较长时，可以提高阶段超时：

```bash
export OCTOS_MCP_TIMEOUT=3600
```

如果失败发生在 MCP 初始化或工具发现阶段，再提高
`OCTOS_MCP_STARTUP_TIMEOUT`。发生超时后 ARC 会终止当前 Octos 子进程，恢复编译
时会创建新进程。

### 提示多个后端互斥

检查命令行和环境中是否还保留了远程 Octos 或 agent-chat 委托配置：

```bash
unset OCTOS_MCP_URL
unset ARC_DELEGATE_TO
```

进度上报到 agent-chat 不等于阶段委托，可以继续使用
`--agent-chat-group` 或 `--agent-chat-to`；只有 `--delegate-to` 会与本地 Octos
冲突。
