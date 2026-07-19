"""Serve mode: run ARC as a dispatchable agent-chat agent.

The worker heartbeats to agent-chat (which auto-registers the agent and marks
it online), polls its inbox, and executes DMs carrying a ``task_request``
schema. Each task is answered with a ``task_result`` reply. Like the progress
reporter, every network failure is swallowed so a flaky backend can only delay
task pickup, never crash the worker.
"""

from __future__ import annotations

import asyncio
import traceback
from typing import Any, Awaitable, Callable

import httpx

TaskRunner = Callable[[dict], Awaitable[dict]]

TASK_REQUEST_KIND = "task_request"
TASK_RESULT_KIND = "task_result"


class AgentChatWorker:
    def __init__(
        self,
        base_url: str,
        *,
        agent_name: str = "arc-compiler",
        task_runner: TaskRunner,
        api_token: str | None = None,
        agent_token: str | None = None,
        poll_interval: float = 5.0,
        heartbeat_interval: float = 60.0,
        timeout: float = 30.0,
    ) -> None:
        self.agent_name = agent_name
        self.task_runner = task_runner
        self.poll_interval = poll_interval
        self.heartbeat_interval = heartbeat_interval
        headers = {}
        if api_token:
            headers["Authorization"] = f"Bearer {api_token}"
        if agent_token:
            headers["X-Agent-Token"] = agent_token
        self._client = httpx.AsyncClient(base_url=base_url, headers=headers, timeout=timeout)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def heartbeat(self) -> bool:
        return await self._request("POST", f"/api/agents/{self.agent_name}/heartbeat", json={}) is not None

    async def run_once(self) -> int:
        """Poll the inbox once and execute every task_request DM. Returns tasks handled."""
        inbox = await self._request("GET", f"/api/inbox/{self.agent_name}")
        if inbox is None:
            return 0
        handled = 0
        for message in inbox.get("dm") or []:
            schema = message.get("schema") or {}
            if schema.get("kind") != TASK_REQUEST_KIND:
                continue
            await self._handle_task(message, schema.get("payload") or {})
            handled += 1
        return handled

    async def run_forever(self, stop_event: asyncio.Event | None = None) -> None:
        stop_event = stop_event or asyncio.Event()
        next_heartbeat = 0.0
        loop = asyncio.get_running_loop()
        while not stop_event.is_set():
            now = loop.time()
            if now >= next_heartbeat:
                await self.heartbeat()
                next_heartbeat = now + self.heartbeat_interval
            await self.run_once()
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=self.poll_interval)
            except asyncio.TimeoutError:
                pass

    async def _handle_task(self, message: dict, payload: dict) -> None:
        requester = message.get("from") or "operator"
        if not payload.get("requirement_dir"):
            await self._reply(
                requester,
                message.get("id"),
                {"ok": False, "error": "task_request payload is missing 'requirement_dir'"},
            )
            return
        try:
            result = await self.task_runner(dict(payload))
        except Exception as exc:
            result = {
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
                "traceback": traceback.format_exc(limit=5),
            }
        if "ok" not in result:
            result = {"ok": True, **result}
        await self._reply(requester, message.get("id"), result)

    async def _reply(self, to: str, reply_to: str | None, result: dict) -> None:
        ok = bool(result.get("ok"))
        summary = f"[arc] task {'completed' if ok else 'FAILED'}"
        body = {
            "from": self.agent_name,
            "to": to,
            "type": "reply",
            "priority": "normal" if ok else "high",
            "summary": summary,
            "full": result.get("error") or summary,
            "reply_to": reply_to,
            "schema": {"kind": TASK_RESULT_KIND, "version": 1, "payload": result},
        }
        await self._request("POST", "/api/messages", json=body)

    async def _request(self, method: str, path: str, json: dict | None = None) -> Any | None:
        try:
            response = await self._client.request(method, path, json=json)
            response.raise_for_status()
            return response.json()
        except Exception:
            return None
