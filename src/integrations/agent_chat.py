"""Best-effort progress reporting to an agent-chat backend.

Forwards ARC log_cb milestones as structured messages (schema kind
``arc_progress``) to agent-chat's ``POST /api/messages``. Reporting must never
break or slow a compilation run: every network failure is swallowed, and the
reporter disables itself after ``max_consecutive_failures`` errors.
"""

from __future__ import annotations

import os
from typing import Awaitable, Callable, Mapping

import httpx

LogCallback = Callable[[str, str, str | None, str | None], Awaitable[None] | None]

FORWARD_STATUSES = {"info", "ok", "warning", "error"}
MILESTONE_AGENTS = {"Compiler"}
SUMMARY_MAX_LEN = 120


class AgentChatReporter:
    def __init__(
        self,
        base_url: str,
        *,
        agent_name: str = "arc-compiler",
        group: str | None = None,
        to: str | None = None,
        api_token: str | None = None,
        run_label: str | None = None,
        timeout: float = 5.0,
        max_consecutive_failures: int = 5,
    ) -> None:
        if bool(group) == bool(to):
            raise ValueError("Provide exactly one of 'group' or 'to'")
        self.agent_name = agent_name
        self.group = group
        self.to = to
        self.run_label = run_label
        self.max_consecutive_failures = max_consecutive_failures
        self._consecutive_failures = 0
        headers = {"Authorization": f"Bearer {api_token}"} if api_token else {}
        self._client = httpx.AsyncClient(base_url=base_url, headers=headers, timeout=timeout)

    @property
    def disabled(self) -> bool:
        return self._consecutive_failures >= self.max_consecutive_failures

    async def aclose(self) -> None:
        await self._client.aclose()

    async def register(self) -> bool:
        return await self._post("/api/agents", {"name": self.agent_name}) is not None

    def should_forward(self, agent_name: str, status: str | None) -> bool:
        if status in FORWARD_STATUSES:
            return True
        return status is None and agent_name in MILESTONE_AGENTS

    def make_log_cb(self, inner: LogCallback | None = None) -> LogCallback:
        async def log_cb(
            agent_name: str,
            message: str,
            status: str | None = None,
            node_id: str | None = None,
        ) -> None:
            if inner is not None:
                result = inner(agent_name, message, status, node_id)
                if hasattr(result, "__await__"):
                    await result
            if self.should_forward(agent_name, status):
                await self.report(agent_name, message, status, node_id)

        return log_cb

    async def report(
        self,
        agent_name: str,
        message: str,
        status: str | None,
        node_id: str | None,
    ) -> None:
        summary = f"[{agent_name}] {message}"
        if len(summary) > SUMMARY_MAX_LEN:
            summary = summary[: SUMMARY_MAX_LEN - 3] + "..."
        body = {
            "from": self.agent_name,
            "type": "inform",
            "priority": "high" if status == "error" else "normal",
            "summary": summary,
            "full": message,
            "schema": {
                "kind": "arc_progress",
                "version": 1,
                "payload": {
                    "agent": agent_name,
                    "status": status,
                    "node_id": node_id,
                    "run": self.run_label,
                },
            },
        }
        if self.group:
            body["group"] = self.group
        else:
            body["to"] = self.to
        await self._post("/api/messages", body)

    async def _post(self, path: str, body: dict) -> httpx.Response | None:
        if self.disabled:
            return None
        try:
            response = await self._client.post(path, json=body)
            response.raise_for_status()
        except Exception:
            self._consecutive_failures += 1
            return None
        self._consecutive_failures = 0
        return response


def build_reporter(
    url: str | None = None,
    group: str | None = None,
    to: str | None = None,
    run_label: str | None = None,
    env: Mapping[str, str] | None = None,
) -> AgentChatReporter | None:
    """Build a reporter from CLI args with env fallbacks; None if not configured."""
    if env is None:
        env = os.environ
    url = url or env.get("AGENT_CHAT_URL")
    if not url:
        return None
    group = group or env.get("AGENT_CHAT_GROUP")
    to = to or env.get("AGENT_CHAT_TO")
    if bool(group) == bool(to):
        raise ValueError(
            "agent-chat reporting needs exactly one target: set --agent-chat-group / "
            "AGENT_CHAT_GROUP or --agent-chat-to / AGENT_CHAT_TO"
        )
    return AgentChatReporter(
        url,
        agent_name=env.get("AGENT_CHAT_AGENT_NAME", "arc-compiler"),
        group=group,
        to=to,
        api_token=env.get("AGENT_CHAT_TOKEN"),
        run_label=run_label,
    )
