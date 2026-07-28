"""Delegate ARC stage work to octos via its MCP server (`octos mcp-serve`).

octos exposes a single MCP tool, ``run_octos_session``, that runs its full
agentic coding loop against a working directory and returns a structured
outcome (artifact path + inline content, validator results, cost, and a typed
error prefix). This delegator packages an ARC stage's prompts into that call,
asks octos to write the stage's JSON result to an ``expected_artifact`` file,
and parses the returned ``artifact_content`` back into the dict the ARC stage
adapters expect.

It retains the legacy ``invoke_stage`` transport interface and is wrapped by
``agents.backend.LocalOctosBackend``. The backend receives ARC's versioned
compiled task, embeds that contract in the Octos prompt, and maps the MCP
result back to the stage adapter. octos brings its own model and API key, so
ARC needs no OpenAI-compatible key in this mode.

Requirements:
- For local compilation, ARC launches and owns:
  ``octos mcp-serve --transport stdio --cwd <ARC output-dir>``
- The legacy remote adapter requires a compatible JSON-RPC HTTP endpoint and
  optional ``OCTOS_MCP_SERVER_TOKEN``. Current Octos Streamable HTTP servers
  should use local stdio mode until the remote client gains session/SSE support.
  ``expected_artifact`` is always resolved relative to the server's working
  directory, which must equal ARC's workspace_root.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
from collections import deque
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit, urlunsplit

import httpx

from integrations.stage_delegation import StageDelegationError

ARTIFACT_SUBDIR = ".arc/delegated"
DEFAULT_CONTRACT = "coding"
MCP_PROTOCOL_VERSION = "2024-11-05"
RUN_OCTOS_SESSION_TOOL = "run_octos_session"


class OctosMcpDelegator:
    def __init__(
        self,
        base_url: str,
        *,
        token: str | None = None,
        contract: str = DEFAULT_CONTRACT,
        endpoint: str = "/mcp",
        artifact_subdir: str = ARTIFACT_SUBDIR,
        timeout: float = 1800.0,
    ) -> None:
        self.contract = contract
        self.endpoint = endpoint
        self.artifact_subdir = artifact_subdir.strip("/")
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        self._client = httpx.AsyncClient(base_url=base_url, headers=headers, timeout=timeout)
        self._request_id = 0

    async def aclose(self) -> None:
        await self._client.aclose()

    async def invoke_stage(
        self,
        *,
        stage: str,
        node_id: str,
        phase: str,
        workspace_root: str,
        system_prompt: str,
        message: str,
        response_schema: dict | None = None,
        compiled_task: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        artifact_rel = f"{self.artifact_subdir}/{stage}-{node_id}.json"
        prompt = self._compose_prompt(
            stage=stage,
            system_prompt=system_prompt,
            message=message,
            artifact_rel=artifact_rel,
            response_schema=response_schema,
            compiled_task=compiled_task,
        )
        body = await self._call_run_session(
            {
                "prompt": prompt,
                "expected_artifact": artifact_rel,
                "artifact_name": "primary",
            }
        )
        content = body.get("artifact_content")
        if content is None:
            raise StageDelegationError(
                f"octos ran stage {stage} for node {node_id} but returned no artifact_content "
                f"(artifact_path={body.get('artifact_path')!r}); ensure the agent wrote {artifact_rel}"
            )
        try:
            parsed = json.loads(content)
        except (json.JSONDecodeError, TypeError):
            if response_schema is None:
                return {"summary": str(content).strip()}
            raise StageDelegationError(
                f"octos stage {stage} artifact was not valid JSON for the required schema: {content[:200]!r}"
            )
        if isinstance(parsed, dict):
            return parsed
        return {"summary": str(parsed)}

    def _compose_prompt(
        self,
        *,
        stage: str,
        system_prompt: str,
        message: str,
        artifact_rel: str,
        response_schema: dict | None,
        compiled_task: dict[str, Any] | None,
    ) -> str:
        parts = [f"[ARC delegated stage: {stage}]"]
        if compiled_task is None:
            parts.extend(
                [
                    "",
                    "=== ROLE (system prompt) ===",
                    system_prompt.strip(),
                    "",
                    "=== TASK ===",
                    message.strip(),
                ]
            )
        else:
            parts.extend(
                [
                    "",
                    "=== ARC COMPILED TASK CONTRACT ===",
                    json.dumps(
                        compiled_task,
                        ensure_ascii=False,
                        indent=2,
                        sort_keys=True,
                    ),
                    "",
                    "Execute the compiled task above. Treat `system_prompt` as your role, "
                    "`message` as the task, and `acceptance` as mandatory completion criteria.",
                ]
            )
        parts.extend(
            [
                "",
                "=== DELIVERABLE ===",
                f"Write your final result as a single file at (relative to the working directory): {artifact_rel}",
            ]
        )
        if response_schema is not None:
            parts.extend(
                [
                    "The file MUST contain a JSON object matching this schema:",
                    json.dumps(response_schema, ensure_ascii=False),
                ]
            )
        else:
            parts.append(
                "The file should contain a short final status line (e.g. IMPLEMENTED ...). "
                "Also make the code changes the task requires directly in the working directory."
            )
        return "\n".join(parts)

    async def _call_run_session(self, task_input: dict) -> dict[str, Any]:
        self._request_id += 1
        request = {
            "jsonrpc": "2.0",
            "id": self._request_id,
            "method": "tools/call",
            "params": {
                "name": "run_octos_session",
                "arguments": {"contract": self.contract, "input": task_input},
            },
        }
        try:
            response = await self._client.post(self.endpoint, json=request)
            response.raise_for_status()
            envelope = response.json()
        except Exception as exc:
            raise StageDelegationError(f"octos mcp-serve call failed: {exc}") from exc

        return self._parse_response_envelope(envelope)

    def _parse_response_envelope(self, envelope: dict[str, Any]) -> dict[str, Any]:
        if isinstance(envelope.get("error"), dict):
            err = envelope["error"]
            raise StageDelegationError(
                f"octos mcp-serve returned JSON-RPC error {err.get('code')}: {err.get('message')}"
            )
        result = envelope.get("result") or {}
        text = self._extract_text(result)
        try:
            body = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            raise StageDelegationError(f"octos mcp-serve returned non-JSON result body: {text[:200]!r}")
        if result.get("isError") or body.get("error"):
            raise StageDelegationError(
                f"octos session failed: {body.get('error') or 'unknown error'}"
            )
        return body

    @staticmethod
    def _extract_text(result: dict) -> str:
        content = result.get("content")
        if isinstance(content, list) and content:
            first = content[0]
            if isinstance(first, dict) and isinstance(first.get("text"), str):
                return first["text"]
        raise StageDelegationError(f"octos mcp-serve result had no text content: {result!r}")


class LocalOctosMcpDelegator(OctosMcpDelegator):
    """Run one local ``octos mcp-serve --transport stdio`` process per ARC compile."""

    def __init__(
        self,
        command: Sequence[str],
        *,
        workspace_root: str,
        contract: str = DEFAULT_CONTRACT,
        artifact_subdir: str = ARTIFACT_SUBDIR,
        timeout: float = 1800.0,
        startup_timeout: float = 30.0,
        shutdown_timeout: float = 3.0,
    ) -> None:
        if not command:
            raise ValueError("local octos command must not be empty")
        self.command = tuple(str(part) for part in command)
        self.workspace_root = str(Path(workspace_root).expanduser().resolve())
        self.process_argv = (
            *self.command,
            "mcp-serve",
            "--transport",
            "stdio",
            "--cwd",
            self.workspace_root,
        )
        self.contract = contract
        self.artifact_subdir = artifact_subdir.strip("/")
        self.timeout = timeout
        self.startup_timeout = startup_timeout
        self.shutdown_timeout = shutdown_timeout
        self._request_id = 0
        self._process: asyncio.subprocess.Process | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._stderr_tail: deque[str] = deque(maxlen=20)
        self._start_lock = asyncio.Lock()
        self._rpc_lock = asyncio.Lock()
        self._initialized = False
        self._closed = False

    @property
    def is_running(self) -> bool:
        return self._process is not None and self._process.returncode is None

    async def start(self) -> None:
        if self._initialized and self.is_running:
            return
        async with self._start_lock:
            if self._initialized and self.is_running:
                return
            if self._closed:
                raise StageDelegationError("local octos delegator is already closed")
            try:
                self._process = await asyncio.create_subprocess_exec(
                    *self.process_argv,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            except (OSError, ValueError) as exc:
                raise StageDelegationError(
                    f"failed to start local octos command {self.process_argv[0]!r}: {exc}"
                ) from exc

            self._stderr_task = asyncio.create_task(self._drain_stderr())
            try:
                initialized = await self._request(
                    "initialize",
                    {
                        "protocolVersion": MCP_PROTOCOL_VERSION,
                        "capabilities": {},
                        "clientInfo": {"name": "arc-compiler", "version": "1.1.0"},
                    },
                    timeout=self.startup_timeout,
                )
                if not isinstance(initialized, dict) or not initialized.get("protocolVersion"):
                    raise StageDelegationError(
                        f"local octos returned an invalid MCP initialize result: {initialized!r}"
                    )
                await self._notify("notifications/initialized")
                tools_result = await self._request(
                    "tools/list",
                    {},
                    timeout=self.startup_timeout,
                )
                tools = tools_result.get("tools") if isinstance(tools_result, dict) else None
                tool_names = {
                    str(tool.get("name"))
                    for tool in (tools or [])
                    if isinstance(tool, dict) and tool.get("name")
                }
                if RUN_OCTOS_SESSION_TOOL not in tool_names:
                    raise StageDelegationError(
                        f"local octos MCP server does not expose required tool "
                        f"{RUN_OCTOS_SESSION_TOOL!r}"
                    )
            except BaseException:
                await self._stop_process()
                raise
            self._initialized = True

    async def aclose(self) -> None:
        self._closed = True
        await self._stop_process()

    async def _call_run_session(self, task_input: dict) -> dict[str, Any]:
        try:
            await self.start()
            envelope = await self._request_envelope(
                "tools/call",
                {
                    "name": RUN_OCTOS_SESSION_TOOL,
                    "arguments": {"contract": self.contract, "input": task_input},
                },
                timeout=self.timeout,
            )
        except StageDelegationError:
            await self._stop_process()
            raise
        except asyncio.CancelledError:
            await self._stop_process()
            raise
        return self._parse_response_envelope(envelope)

    async def _request(
        self,
        method: str,
        params: dict[str, Any],
        *,
        timeout: float,
    ) -> dict[str, Any]:
        envelope = await self._request_envelope(method, params, timeout=timeout)
        if isinstance(envelope.get("error"), dict):
            error = envelope["error"]
            raise StageDelegationError(
                f"local octos MCP {method} failed "
                f"({error.get('code')}): {error.get('message')}"
            )
        result = envelope.get("result")
        if not isinstance(result, dict):
            raise StageDelegationError(
                f"local octos MCP {method} returned no object result: {envelope!r}"
            )
        return result

    async def _request_envelope(
        self,
        method: str,
        params: dict[str, Any],
        *,
        timeout: float,
    ) -> dict[str, Any]:
        async with self._rpc_lock:
            self._request_id += 1
            request_id = self._request_id
            await self._write_envelope(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": method,
                    "params": params,
                }
            )
            try:
                return await asyncio.wait_for(
                    self._read_response(request_id),
                    timeout=timeout,
                )
            except TimeoutError as exc:
                await self._stop_process()
                raise StageDelegationError(
                    f"local octos MCP {method} timed out after {timeout:.2f}s"
                ) from exc

    async def _notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        async with self._rpc_lock:
            envelope: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
            if params is not None:
                envelope["params"] = params
            await self._write_envelope(envelope)

    async def _write_envelope(self, envelope: dict[str, Any]) -> None:
        process = self._process
        if process is None or process.stdin is None or process.returncode is not None:
            raise StageDelegationError(self._process_failure("local octos process is not running"))
        try:
            process.stdin.write(
                (json.dumps(envelope, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
            )
            await process.stdin.drain()
        except (BrokenPipeError, ConnectionError, OSError) as exc:
            raise StageDelegationError(
                self._process_failure(f"failed to write to local octos: {exc}")
            ) from exc

    async def _read_response(self, request_id: int) -> dict[str, Any]:
        process = self._process
        if process is None or process.stdout is None:
            raise StageDelegationError(self._process_failure("local octos stdout is unavailable"))
        while True:
            raw = await process.stdout.readline()
            if not raw:
                with contextlib.suppress(Exception):
                    await process.wait()
                raise StageDelegationError(
                    self._process_failure("local octos exited before returning an MCP response")
                )
            try:
                envelope = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise StageDelegationError(
                    f"local octos returned invalid JSON on stdout: "
                    f"{raw.decode(errors='replace')[:200]!r}"
                ) from exc
            if not isinstance(envelope, dict):
                continue
            if "id" not in envelope:
                continue
            if envelope.get("id") != request_id:
                raise StageDelegationError(
                    f"local octos returned unexpected response id "
                    f"{envelope.get('id')!r}; expected {request_id}"
                )
            return envelope

    async def _drain_stderr(self) -> None:
        process = self._process
        if process is None or process.stderr is None:
            return
        while True:
            raw = await process.stderr.readline()
            if not raw:
                return
            line = raw.decode(errors="replace").strip()
            if line:
                self._stderr_tail.append(line)

    async def _stop_process(self) -> None:
        process = self._process
        self._initialized = False
        if process is not None:
            if process.stdin is not None and not process.stdin.is_closing():
                process.stdin.close()
                with contextlib.suppress(BrokenPipeError, ConnectionError):
                    await process.stdin.wait_closed()
            if process.returncode is None:
                try:
                    await asyncio.wait_for(process.wait(), timeout=self.shutdown_timeout)
                except TimeoutError:
                    with contextlib.suppress(ProcessLookupError):
                        process.terminate()
                    if process.returncode is None:
                        try:
                            await asyncio.wait_for(process.wait(), timeout=self.shutdown_timeout)
                        except TimeoutError:
                            with contextlib.suppress(ProcessLookupError):
                                process.kill()
                            await process.wait()

        stderr_task = self._stderr_task
        if stderr_task is not None:
            if not stderr_task.done():
                stderr_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await stderr_task
        self._stderr_task = None
        self._process = None

    def _process_failure(self, message: str) -> str:
        if not self._stderr_tail:
            return message
        return f"{message}; octos stderr: {' | '.join(self._stderr_tail)}"


def build_octos_delegator(
    url: str | None = None,
    env: Mapping[str, str] | None = None,
) -> OctosMcpDelegator | None:
    """Build an octos delegator from a CLI arg with env fallbacks; None if unset."""
    if env is None:
        env = os.environ
    url = url or env.get("OCTOS_MCP_URL")
    if not url:
        return None
    split = urlsplit(url)
    origin = urlunsplit((split.scheme, split.netloc, "", "", ""))
    endpoint = split.path if split.path and split.path != "/" else "/mcp"
    return OctosMcpDelegator(
        origin,
        endpoint=endpoint,
        token=env.get("OCTOS_MCP_SERVER_TOKEN"),
        contract=env.get("OCTOS_MCP_CONTRACT", DEFAULT_CONTRACT),
        timeout=float(env.get("OCTOS_MCP_TIMEOUT", "1800")),
    )


def build_local_octos_delegator(
    binary: str | None,
    *,
    workspace_root: str,
    env: Mapping[str, str] | None = None,
) -> LocalOctosMcpDelegator:
    """Resolve a local octos binary and build a reusable stdio MCP delegator."""
    if env is None:
        env = os.environ
    candidate = str(binary or env.get("OCTOS_BIN") or "octos").strip()
    expanded = str(Path(candidate).expanduser())
    if os.path.isabs(expanded) or os.sep in expanded:
        path = Path(expanded).resolve()
        if not path.is_file() or not os.access(path, os.X_OK):
            raise ValueError(f"local octos binary is missing or not executable: {candidate}")
        resolved = str(path)
    else:
        found = shutil.which(expanded)
        if not found:
            raise ValueError(f"local octos binary was not found on PATH: {candidate}")
        resolved = found
    return LocalOctosMcpDelegator(
        (resolved,),
        workspace_root=workspace_root,
        contract=env.get("OCTOS_MCP_CONTRACT", DEFAULT_CONTRACT),
        timeout=float(env.get("OCTOS_MCP_TIMEOUT", "1800")),
        startup_timeout=float(env.get("OCTOS_MCP_STARTUP_TIMEOUT", "30")),
    )
