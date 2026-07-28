"""Compiled agent tasks and replaceable ARC execution backends.

Stage adapters are ARC's compilation front-end: they assemble requirement
context, prompts, response schemas, and acceptance conditions. Backends only
execute the resulting :class:`CompiledAgentTask`.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from agents.context import AgentRuntimeContext
from agents.factory import build_stage_agent
from agents.runners import ainvoke_stage_agent


LogCallback = Callable[
    [str, str, str | None, str | None],
    Awaitable[None] | None,
]
AGENT_TASK_SCHEMA = "arc.agent-task.v1"
AGENT_TASK_SUBDIR = ".arc/agent_tasks"
_SAFE_FILENAME = re.compile(r"[^A-Za-z0-9._-]+")
_SECRET_KEYS = {
    "api_key",
    "apikey",
    "authorization",
    "bearer",
    "password",
    "secret",
    "token",
}


@dataclass(slots=True)
class CompiledAgentTask:
    """A versioned stage execution package produced by ARC's front-end.

    Fields through ``acceptance`` form the portable JSON contract. The
    remaining fields are explicitly process-local inputs for the built-in
    backend and are never persisted.
    """

    task_id: str
    stage: str
    backend_agent_name: str
    node_id: str
    phase: str
    app_type: str
    workspace_root: str
    requirement_path: str
    system_prompt: str
    message: str
    response_schema: dict[str, Any] | None
    inputs: dict[str, Any]
    acceptance: dict[str, Any]
    thread_id: str = ""
    test_type: str = ""
    skills: tuple[str, ...] = ()
    model: str | object | None = field(default=None, repr=False)
    response_format: object | None = field(default=None, repr=False)
    runtime_tools: tuple[object, ...] = field(default=(), repr=False)
    log_cb: LogCallback | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if not self.task_id.strip():
            raise ValueError("compiled agent task requires a task_id")
        if not self.stage.strip():
            raise ValueError("compiled agent task requires a stage")
        if not self.node_id.strip():
            raise ValueError("compiled agent task requires a node_id")
        if not self.phase.strip():
            raise ValueError("compiled agent task requires a phase")
        self.workspace_root = str(Path(self.workspace_root).expanduser().resolve())
        if self.requirement_path:
            self.requirement_path = str(Path(self.requirement_path).expanduser().resolve())
        if not self.thread_id:
            suffix = f":{self.test_type}" if self.test_type else ""
            self.thread_id = f"{self.node_id}:{self.phase}:{self.stage}{suffix}"

    def to_payload(self) -> dict[str, Any]:
        """Return the portable backend contract without process-local objects."""

        payload = {
            "schema": AGENT_TASK_SCHEMA,
            "task_id": self.task_id,
            "stage": self.stage,
            "backend_agent_name": self.backend_agent_name,
            "node_id": self.node_id,
            "phase": self.phase,
            "app_type": self.app_type,
            "workspace_root": self.workspace_root,
            "requirement_path": self.requirement_path,
            "thread_id": self.thread_id,
            "test_type": self.test_type,
            "system_prompt": self.system_prompt,
            "message": self.message,
            "response_schema": self.response_schema,
            "inputs": self.inputs,
            "acceptance": self.acceptance,
            "skills": list(self.skills),
        }
        sanitized = _redact_secret_values(payload)
        # Fail before touching the filesystem if an adapter supplied a value
        # that is not part of the portable JSON contract.
        json.dumps(sanitized, ensure_ascii=False, sort_keys=True)
        return sanitized

    def persist(self) -> Path:
        """Atomically save the latest compiled form of this task."""

        task_dir = Path(self.workspace_root) / AGENT_TASK_SUBDIR
        task_dir.mkdir(parents=True, exist_ok=True)
        safe_id = _SAFE_FILENAME.sub("-", self.task_id).strip("-._") or "task"
        digest = hashlib.sha256(self.task_id.encode("utf-8")).hexdigest()[:10]
        path = task_dir / f"{safe_id[:96]}-{digest}.json"
        temporary = path.with_suffix(f"{path.suffix}.tmp")
        serialized = json.dumps(
            self.to_payload(),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        temporary.write_text(f"{serialized}\n", encoding="utf-8")
        os.replace(temporary, path)
        return path


class AgentBackend(ABC):
    """Execution backend for a compiled ARC agent task."""

    name = "backend"
    is_external = False

    async def execute(self, task: CompiledAgentTask) -> dict[str, Any]:
        task.persist()
        result = await self._execute(task)
        if not isinstance(result, dict):
            raise TypeError(
                f"agent backend {self.name!r} returned {type(result).__name__}; expected dict"
            )
        return result

    @abstractmethod
    async def _execute(self, task: CompiledAgentTask) -> dict[str, Any]:
        raise NotImplementedError

    async def aclose(self) -> None:
        """Release backend resources. Built-in execution owns none."""


class BuiltinAgentBackend(AgentBackend):
    """Execute compiled tasks with ARC's existing deep-agent runtime."""

    name = "builtin"

    async def _execute(self, task: CompiledAgentTask) -> dict[str, Any]:
        if task.model is None:
            raise ValueError(f"compiled task {task.task_id!r} has no built-in model")
        agent = build_stage_agent(
            name=task.backend_agent_name,
            model=task.model,
            system_prompt=task.system_prompt,
            response_format=task.response_format,
            workspace_root=task.workspace_root,
            writable_roots=[task.workspace_root],
            skills=list(task.skills),
            memory=[],
            tools=list(task.runtime_tools),
        )
        return await ainvoke_stage_agent(
            agent,
            message=task.message,
            context=AgentRuntimeContext(
                node_id=task.node_id,
                phase=task.phase,
                app_type=task.app_type,
                workspace_root=task.workspace_root,
                requirement_path=task.requirement_path,
                test_type=task.test_type,
            ),
            thread_id=task.thread_id,
            label=task.stage,
            log_cb=task.log_cb,
        )


class DelegatingAgentBackend(AgentBackend):
    """Adapt a legacy stage delegator to the compiled backend contract."""

    name = "delegated"
    is_external = True

    def __init__(self, delegator: Any, *, name: str = "delegated") -> None:
        self.delegator = delegator
        self.name = name
        self._closed = False

    async def _execute(self, task: CompiledAgentTask) -> dict[str, Any]:
        return await self.delegator.invoke_stage(
            stage=task.stage,
            node_id=task.node_id,
            phase=task.phase,
            workspace_root=task.workspace_root,
            system_prompt=task.system_prompt,
            message=task.message,
            response_schema=task.response_schema,
        )

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self.delegator.aclose()


class LocalOctosBackend(DelegatingAgentBackend):
    """Execute compiled ARC tasks through one local Octos stdio MCP process."""

    def __init__(self, delegator: Any) -> None:
        super().__init__(delegator, name="octos-local")

    async def _execute(self, task: CompiledAgentTask) -> dict[str, Any]:
        return await self.delegator.invoke_stage(
            stage=task.stage,
            node_id=task.node_id,
            phase=task.phase,
            workspace_root=task.workspace_root,
            system_prompt=task.system_prompt,
            message=task.message,
            response_schema=task.response_schema,
            compiled_task=task.to_payload(),
        )


def _redact_secret_values(value: Any) -> Any:
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            normalized = str(key).strip().lower().replace("-", "_")
            redacted[str(key)] = (
                "[REDACTED]"
                if normalized in _SECRET_KEYS or normalized.endswith("_api_key")
                else _redact_secret_values(item)
            )
        return redacted
    if isinstance(value, list):
        return [_redact_secret_values(item) for item in value]
    if isinstance(value, tuple):
        return [_redact_secret_values(item) for item in value]
    return value
