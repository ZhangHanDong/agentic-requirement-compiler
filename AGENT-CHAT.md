# agent-chat Integration

ARC integrates with [agent-chat](https://github.com/ZhangHanDong/agent-chat) at three levels.
All of them are configured with the same base env vars:

| Env | Meaning |
|---|---|
| `AGENT_CHAT_URL` | agent-chat backend base URL (e.g. `http://127.0.0.1:8090`) |
| `AGENT_CHAT_TOKEN` | operator bearer token (`API_TOKEN` of the backend), optional |
| `AGENT_CHAT_AGENT_TOKEN` | per-agent token for `X-Agent-Token`, optional |
| `AGENT_CHAT_AGENT_NAME` | ARC's agent name in the mesh (default `arc-compiler`) |

## 1. Progress reporting (one-way)

Forward compilation milestones and errors as `arc_progress` structured messages:

```bash
python src/main.py compile <requirement-dir> -o <output-dir> \
  --agent-chat-url http://127.0.0.1:8090 \
  --agent-chat-group arc        # or --agent-chat-to <agent|human>
```

Best-effort: network failures never affect the compile; the reporter disables
itself after 5 consecutive errors.

## 2. Serve mode (ARC as a dispatchable agent)

```bash
python src/main.py serve --agent-chat-url http://127.0.0.1:8090
```

ARC heartbeats (auto-registering and showing online), polls its inbox, and
executes DMs with `schema.kind == "task_request"`:

```python
send_message(
  to="arc-compiler",
  summary="compile ticketbooking demo",
  full="...",
  schema={"kind": "task_request", "payload": {
    "requirement_dir": "/abs/path/to/requirement-dir",   # required
    "app_type": "web", "output_dir": "...", "web_port": 3000, "clear_all": false
  }}
)
```

Each task is answered with a `task_result` reply (`{ok, failed_nodes,
output_dir, log_path}`).

## 3. Stage delegation (run stages on Claude Code / Codex, no API key)

Delegate the three stage agents (InterfaceDesigner / TestGenerator /
TestDrivenDeveloper) to an agent-chat CLI agent running on your subscription,
instead of calling an OpenAI-compatible API:

```bash
python src/main.py compile <requirement-dir> -o <output-dir> \
  --agent-chat-url http://127.0.0.1:8090 \
  --delegate-to claude-implementer      # or ARC_DELEGATE_TO env
```

Protocol (`arc_stage`): ARC DMs the implementer a `task_request` whose `full`
text contains the stage's system prompt, task context, and reply convention;
the payload carries `{protocol: "arc_stage", stage, node_id, phase, workspace,
response_schema}`. The implementer works directly in the shared workspace
directory and replies `task_result` with `{ok, output}` (`output` matching the
stage's JSON schema, or a status string for TDD).

Guarantees:

- Test verification stays system-owned: after a delegated TDD stage, ARC runs
  the node's tests itself; `IMPLEMENTED` is only accepted on exit code 0.
- Reply correlation uses `reply_to` + kinds-filtered inbox previews, which
  never advance ARC's inbox cursor.
- Knobs: `ARC_DELEGATE_TIMEOUT` (default 1800s per stage),
  `ARC_DELEGATE_POLL_INTERVAL` (default 3s).
- Works combined with `serve` (tasks run sequentially, so delegation polls
  never race the worker's inbox reads).

Limitations: screenshot visual analysis still uses `VISUAL_API_KEY` when the
requirement references screenshots; the implementer agent must run on the same
machine (shared filesystem) and have permission to edit the workspace.

## 4. Stage delegation to local octos (stdio MCP, no agent-chat)

Delegate the stage agents to [Octos](https://github.com/octos-org/octos) via its MCP
server instead of going through agent-chat. Octos runs its own agentic coding
loop with its own model + key, so ARC needs no OpenAI-compatible key for those
three stages. Screenshot analysis remains separately configured as noted below.

For a local compile, ARC launches and owns one octos stdio subprocess, reuses
it for every delegated stage, and closes it when compilation ends:

```bash
python src/main.py compile <requirement-dir> -o <output-dir> \
  --agent-backend octos-local \
  --octos-bin /path/to/octos
```

`--octos-local` remains a compatibility alias. `--octos-bin` defaults to
`OCTOS_BIN`, then to `octos` on `PATH`; specifying the flag also selects the
local backend. `ARC_AGENT_BACKEND=octos-local` is the preferred environment
configuration, while `ARC_OCTOS_LOCAL=1` remains supported. ARC starts the
equivalent of:

```bash
octos mcp-serve --transport stdio --cwd <output-dir>
```

No MCP port or bearer token is needed. Octos still needs its own local provider
configuration. Runtime knobs: `OCTOS_MCP_CONTRACT` (default `coding`),
`OCTOS_MCP_TIMEOUT` (default 1800s per stage), and
`OCTOS_MCP_STARTUP_TIMEOUT` (default 30s for initialize/tool discovery).

Mechanism: each adapter compiles an `arc.agent-task.v1` package and saves it
under `.arc/agent_tasks/`. `LocalOctosBackend` sends that same contract through
Octos's single `run_octos_session` MCP tool with
`{contract, input:{prompt, expected_artifact, artifact_name}}`. The prompt
carries the compiled task, including its system prompt, task input, output
schema and acceptance conditions, and instructs octos to write its JSON result
to `expected_artifact` (`.arc/delegated/<stage>-<node>.json`, relative to
Octos's `--cwd`). ARC reads the returned inline `artifact_content`, parses it
into the stage output, and — for the TDD stage — still runs the node's tests
itself before accepting `IMPLEMENTED`. Octos's typed error prefixes
(`contract_failed:`, `artifact_missing:`, `llm_error:`, …) surface as
`StageDelegationError`.

`--agent-backend builtin`, local Octos, remote Octos, and agent-chat delegation
are mutually exclusive execution choices. The workflow injects one backend
instance into all three stage adapters; the legacy `set_stage_delegator` API is
retained only for compatibility. Local subprocess mode is currently available
on `compile`; the resident `serve` worker does not yet manage a per-workspace
local Octos process pool.

The existing `--octos-mcp` option remains for compatible remote JSON-RPC HTTP
endpoints. Current Octos releases expose MCP Streamable HTTP with session/SSE
semantics, so use `--agent-backend octos-local` for direct interoperability
until ARC's remote transport is upgraded.
