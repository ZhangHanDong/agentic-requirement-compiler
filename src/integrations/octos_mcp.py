"""Delegate ARC stage work to octos via its MCP server (`octos mcp-serve`).

octos exposes a single MCP tool, ``run_octos_session``, that runs its full
agentic coding loop against a working directory and returns a structured
outcome (artifact path + inline content, validator results, cost, and a typed
error prefix). This delegator packages an ARC stage's prompts into that call,
asks octos to write the stage's JSON result to an ``expected_artifact`` file,
and parses the returned ``artifact_content`` back into the dict the ARC stage
adapters expect.

It implements the same ``invoke_stage`` interface as
``stage_delegation.StageDelegator``, so setting one via
``stage_delegation.set_stage_delegator`` makes the three stage adapters
delegate to octos with no adapter changes. octos brings its own model and API
key, so ARC needs no OpenAI-compatible key in this mode.

Requirements:
- Launch octos with a matching workspace root:
  ``octos mcp-serve --transport http --bind 127.0.0.1:4033 --cwd <ARC output-dir>``
  with ``OCTOS_MCP_SERVER_TOKEN`` set. ``expected_artifact`` is resolved
  relative to octos's ``--cwd``, which must equal ARC's workspace_root.
"""

from __future__ import annotations

import json
import os
from typing import Any, Mapping
from urllib.parse import urlsplit, urlunsplit

import httpx

from integrations.stage_delegation import StageDelegationError

ARTIFACT_SUBDIR = ".arc/delegated"
DEFAULT_CONTRACT = "coding"


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
    ) -> dict[str, Any]:
        artifact_rel = f"{self.artifact_subdir}/{stage}-{node_id}.json"
        prompt = self._compose_prompt(
            stage=stage,
            system_prompt=system_prompt,
            message=message,
            artifact_rel=artifact_rel,
            response_schema=response_schema,
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
    ) -> str:
        parts = [
            f"[ARC delegated stage: {stage}]",
            "",
            "=== ROLE (system prompt) ===",
            system_prompt.strip(),
            "",
            "=== TASK ===",
            message.strip(),
            "",
            "=== DELIVERABLE ===",
            f"Write your final result as a single file at (relative to the working directory): {artifact_rel}",
        ]
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
