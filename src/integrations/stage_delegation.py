"""Delegate ARC stage work to an agent-chat CLI agent (Claude Code / Codex).

Instead of running a deepagents loop against an OpenAI-compatible API, a
StageDelegator packages the stage's prompts into a ``task_request`` DM to a
configured implementer agent, which works directly in the shared workspace on
the same machine and replies with a ``task_result`` carrying the stage's JSON
output. Test verification stays system-owned in ARC regardless of who wrote
the code.

Unlike the progress reporter, delegation failures are *not* swallowed: a
stage that cannot be executed must fail loudly so the workflow can record the
node as failed.
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any, Mapping

import httpx

TASK_REQUEST_KIND = "task_request"
TASK_RESULT_KIND = "task_result"
PROTOCOL = "arc_stage"


class StageDelegationError(RuntimeError):
    pass


class StageDelegator:
    def __init__(
        self,
        base_url: str,
        *,
        implementer: str,
        agent_name: str = "arc-compiler",
        api_token: str | None = None,
        agent_token: str | None = None,
        poll_interval: float = 3.0,
        timeout: float = 1800.0,
    ) -> None:
        self.implementer = implementer
        self.agent_name = agent_name
        self.poll_interval = poll_interval
        self.timeout = timeout
        headers = {}
        if api_token:
            headers["Authorization"] = f"Bearer {api_token}"
        if agent_token:
            headers["X-Agent-Token"] = agent_token
        self._client = httpx.AsyncClient(base_url=base_url, headers=headers, timeout=30.0)

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
        instructions = self._compose_instructions(
            stage=stage,
            node_id=node_id,
            workspace_root=workspace_root,
            system_prompt=system_prompt,
            message=message,
            response_schema=response_schema,
        )
        request_id = await self._send_task(
            stage=stage,
            node_id=node_id,
            phase=phase,
            workspace_root=workspace_root,
            instructions=instructions,
            response_schema=response_schema,
        )
        payload = await self._await_reply(request_id)
        if not payload.get("ok"):
            raise StageDelegationError(
                f"Delegated stage {stage} for node {node_id} failed: "
                f"{payload.get('error') or 'implementer reported failure'}"
            )
        output = payload.get("output")
        if isinstance(output, dict):
            return output
        if output is None:
            return {}
        return {"summary": str(output)}

    def _compose_instructions(
        self,
        *,
        stage: str,
        node_id: str,
        workspace_root: str,
        system_prompt: str,
        message: str,
        response_schema: dict | None,
    ) -> str:
        parts = [
            f"[ARC delegated stage task] stage={stage} node={node_id}",
            f"Work directly in this workspace directory on this machine: {workspace_root}",
            "",
            "=== ROLE (system prompt) ===",
            system_prompt.strip(),
            "",
            "=== TASK ===",
            message.strip(),
            "",
            "=== HOW TO REPLY ===",
            "When the task is complete, reply to THIS message via send_message with:",
            f"  to: '{self.agent_name}', type: 'reply', reply_to: <this message's id>,",
            "  schema: {\"kind\": \"" + TASK_RESULT_KIND + "\", \"payload\": {\"ok\": true, \"output\": <output>}}",
            "On failure reply with payload {\"ok\": false, \"error\": \"<what went wrong>\"}.",
        ]
        if response_schema is not None:
            parts.extend(
                [
                    "The 'output' field MUST be a JSON object matching this schema:",
                    json.dumps(response_schema, ensure_ascii=False),
                ]
            )
        else:
            parts.append("The 'output' field should be a short final status text (string is fine).")
        return "\n".join(parts)

    async def _send_task(
        self,
        *,
        stage: str,
        node_id: str,
        phase: str,
        workspace_root: str,
        instructions: str,
        response_schema: dict | None,
    ) -> str:
        body = {
            "from": self.agent_name,
            "to": self.implementer,
            "type": "request",
            "priority": "high",
            "summary": f"[arc] {stage} for node {node_id}",
            "full": instructions,
            "schema": {
                "kind": TASK_REQUEST_KIND,
                "version": 1,
                "payload": {
                    "protocol": PROTOCOL,
                    "stage": stage,
                    "node_id": node_id,
                    "phase": phase,
                    "workspace": workspace_root,
                    "response_schema": response_schema,
                },
            },
        }
        try:
            response = await self._client.post("/api/messages", json=body)
            response.raise_for_status()
            data = response.json()
        except Exception as exc:
            raise StageDelegationError(f"Failed to send delegated stage task: {exc}") from exc
        request_id = data.get("id")
        if not request_id:
            raise StageDelegationError(f"agent-chat send returned no message id: {data}")
        return str(request_id)

    async def _await_reply(self, request_id: str) -> dict[str, Any]:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.timeout
        while True:
            reply = await self._poll_reply(request_id)
            if reply is not None:
                return reply
            if loop.time() >= deadline:
                raise StageDelegationError(
                    f"Delegated stage task {request_id} timed out after {self.timeout:.0f}s "
                    f"waiting for a task_result reply from '{self.implementer}'"
                )
            await asyncio.sleep(self.poll_interval)

    async def _poll_reply(self, request_id: str) -> dict[str, Any] | None:
        # kinds-filtered reads are preview-only: they never advance the inbox
        # cursor, so other unread messages are left untouched.
        try:
            response = await self._client.get(
                f"/api/inbox/{self.agent_name}",
                params={"kinds": TASK_RESULT_KIND},
            )
            response.raise_for_status()
            inbox = response.json()
        except Exception:
            return None
        for message in inbox.get("dm") or []:
            if message.get("reply_to") != request_id:
                continue
            schema = message.get("schema") or {}
            if schema.get("kind") != TASK_RESULT_KIND:
                continue
            payload = schema.get("payload")
            return payload if isinstance(payload, dict) else {"ok": False, "error": "malformed task_result payload"}
        return None


def build_stage_delegator(
    url: str | None = None,
    implementer: str | None = None,
    env: Mapping[str, str] | None = None,
) -> StageDelegator | None:
    """Build a delegator from CLI args with env fallbacks; None if not configured."""
    if env is None:
        env = os.environ
    url = url or env.get("AGENT_CHAT_URL")
    implementer = implementer or env.get("ARC_DELEGATE_TO")
    if not url or not implementer:
        return None
    return StageDelegator(
        url,
        implementer=implementer,
        agent_name=env.get("AGENT_CHAT_AGENT_NAME", "arc-compiler"),
        api_token=env.get("AGENT_CHAT_TOKEN"),
        agent_token=env.get("AGENT_CHAT_AGENT_TOKEN"),
        poll_interval=float(env.get("ARC_DELEGATE_POLL_INTERVAL", "3")),
        timeout=float(env.get("ARC_DELEGATE_TIMEOUT", "1800")),
    )


_stage_delegator: StageDelegator | None = None


def set_stage_delegator(delegator: StageDelegator | None) -> None:
    global _stage_delegator
    _stage_delegator = delegator


def get_stage_delegator() -> StageDelegator | None:
    return _stage_delegator
