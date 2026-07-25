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

## 4. Stage delegation to octos (direct MCP, no agent-chat)

Delegate the stage agents to [octos](https://github.com/.../octos) via its MCP
server instead of going through agent-chat. octos runs its own agentic coding
loop with its own model + key, so ARC needs no OpenAI-compatible key.

Launch octos as an MCP server whose working directory is ARC's output dir:

```bash
OCTOS_MCP_SERVER_TOKEN=secret \
  octos mcp-serve --transport http --bind 127.0.0.1:4033 --cwd <ARC-output-dir>
```

Then run ARC pointing at it:

```bash
OCTOS_MCP_SERVER_TOKEN=secret \
python src/main.py compile <requirement-dir> -o <output-dir> \
  --octos-mcp http://127.0.0.1:4033/mcp
```

Env fallbacks: `OCTOS_MCP_URL`, `OCTOS_MCP_SERVER_TOKEN`, `OCTOS_MCP_CONTRACT`
(default `coding`), `OCTOS_MCP_TIMEOUT` (default 1800s per stage).

Mechanism: ARC calls octos's single `run_octos_session` MCP tool with
`{contract, input:{prompt, expected_artifact, artifact_name}}`. The prompt
carries the stage's system prompt + task and instructs octos to write its JSON
result to `expected_artifact` (`.arc/delegated/<stage>-<node>.json`, relative to
octos's `--cwd`). ARC reads the returned inline `artifact_content`, parses it
into the stage output, and — for the TDD stage — still runs the node's tests
itself before accepting `IMPLEMENTED`. octos's typed error prefixes
(`contract_failed:`, `artifact_missing:`, `llm_error:`, …) surface as
`StageDelegationError`.

`--octos-mcp` and `--delegate-to` are mutually exclusive (both drive the same
`set_stage_delegator` seam). The octos working dir MUST equal ARC's output dir
so `expected_artifact` resolves to the shared workspace.
