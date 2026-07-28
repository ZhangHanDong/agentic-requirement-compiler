from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest


class RecordingBackend:
    name = "recording"
    is_external = True

    def __init__(self, outputs: dict[str, dict] | None = None) -> None:
        self.outputs = outputs or {}
        self.tasks = []
        self.close_calls = 0

    async def execute(self, task):
        self.tasks.append(task)
        return self.outputs.get(task.stage, {"summary": "ok"})

    async def aclose(self) -> None:
        self.close_calls += 1


@pytest.fixture
def quiet_context(monkeypatch, tmp_path):
    from context.context_pipeline import context_pipeline

    monkeypatch.setattr(
        context_pipeline,
        "build_agent_context_split",
        lambda **kwargs: ("static context", "dynamic context"),
    )
    monkeypatch.setattr(
        context_pipeline,
        "get_interface_contract_context",
        lambda node_id: f"interface contract for {node_id}",
    )
    return tmp_path


async def test_compiled_agent_task_persists_versioned_json_without_runtime_objects(tmp_path):
    from agents.backend import AgentBackend, CompiledAgentTask

    class SecretModel:
        api_key = "must-not-be-persisted"

    class EchoBackend(AgentBackend):
        name = "echo"

        async def _execute(self, task):
            return {"summary": "done"}

    task = CompiledAgentTask(
        task_id="REQ-1:DESIGN:InterfaceDesigner",
        stage="InterfaceDesigner",
        backend_agent_name="interface_designer",
        node_id="REQ-1",
        phase="DESIGN",
        app_type="web",
        workspace_root=str(tmp_path),
        requirement_path=str(tmp_path / "requirements.yaml"),
        system_prompt="design role",
        message="design this requirement",
        response_schema={"type": "object"},
        inputs={"requirement": {"id": "REQ-1", "title": "Tickets"}},
        acceptance={"response_schema_required": True},
        model=SecretModel(),
        response_format=object(),
        runtime_tools=(lambda: None,),
    )

    result = await EchoBackend().execute(task)

    assert result == {"summary": "done"}
    saved = list((tmp_path / ".arc" / "agent_tasks").glob("*.json"))
    assert len(saved) == 1
    payload = json.loads(saved[0].read_text(encoding="utf-8"))
    assert payload["schema"] == "arc.agent-task.v1"
    assert payload["task_id"] == task.task_id
    assert payload["stage"] == "InterfaceDesigner"
    assert payload["node_id"] == "REQ-1"
    assert payload["phase"] == "DESIGN"
    assert payload["system_prompt"] == "design role"
    assert payload["message"] == "design this requirement"
    assert payload["inputs"]["requirement"]["id"] == "REQ-1"
    assert payload["acceptance"]["response_schema_required"] is True
    assert payload["response_schema"] == {"type": "object"}
    serialized = json.dumps(payload)
    assert "must-not-be-persisted" not in serialized
    assert "runtime_tools" not in serialized
    assert "response_format" not in serialized
    assert "model" not in serialized


async def test_builtin_agent_backend_executes_compiled_task_with_runtime_context(
    monkeypatch,
    tmp_path,
):
    import agents.backend as backend_mod

    captured: dict = {}
    fake_agent = object()

    def fake_build_stage_agent(**kwargs):
        captured["build"] = kwargs
        return fake_agent

    async def fake_ainvoke_stage_agent(agent, **kwargs):
        captured["agent"] = agent
        captured["invoke"] = kwargs
        return {"summary": "built in"}

    monkeypatch.setattr(backend_mod, "build_stage_agent", fake_build_stage_agent)
    monkeypatch.setattr(backend_mod, "ainvoke_stage_agent", fake_ainvoke_stage_agent)

    runtime_tool = lambda: None
    task = backend_mod.CompiledAgentTask(
        task_id="REQ-2:IMPLEMENT:TestDrivenDeveloper:Unit",
        stage="TestDrivenDeveloper",
        backend_agent_name="test_driven_developer",
        node_id="REQ-2",
        phase="IMPLEMENT",
        app_type="cli",
        workspace_root=str(tmp_path),
        requirement_path=str(tmp_path / "requirements.yaml"),
        system_prompt="implement role",
        message="implement this task",
        response_schema=None,
        inputs={"test_type": "Unit"},
        acceptance={"system_tests": True},
        model="openai:test-model",
        response_format=None,
        skills=("/skills/tdd-test-failure-repair/",),
        runtime_tools=(runtime_tool,),
        test_type="Unit",
        thread_id="REQ-2:IMPLEMENT:TestDrivenDeveloper:Unit",
    )

    output = await backend_mod.BuiltinAgentBackend().execute(task)

    assert output == {"summary": "built in"}
    assert captured["agent"] is fake_agent
    assert captured["build"]["name"] == "test_driven_developer"
    assert captured["build"]["model"] == "openai:test-model"
    assert captured["build"]["workspace_root"] == str(tmp_path)
    assert captured["build"]["tools"] == [runtime_tool]
    assert captured["invoke"]["thread_id"] == task.thread_id
    assert captured["invoke"]["message"] == task.message
    context = captured["invoke"]["context"]
    assert context.node_id == "REQ-2"
    assert context.phase == "IMPLEMENT"
    assert context.app_type == "cli"
    assert context.workspace_root == str(tmp_path)
    assert context.test_type == "Unit"


def test_workflow_injects_one_backend_into_all_stage_adapters(tmp_path):
    from core.workflow import ARCWorkflowManager

    backend = RecordingBackend()
    workflow = ARCWorkflowManager(
        workspace_path=str(tmp_path / "workspace"),
        requirement_path=str(tmp_path / "requirements.yaml"),
        app_type="cli",
        agent_backend=backend,
    )

    assert workflow.agent_backend is backend
    assert workflow.interface_designer.agent_backend is backend
    assert workflow.test_generator.agent_backend is backend
    assert workflow.test_driven_developer.agent_backend is backend


def test_agent_backend_cli_selects_local_octos_and_preserves_legacy_aliases(
    tmp_path,
    monkeypatch,
):
    from agents.backend import LocalOctosBackend
    from main import build_parser, resolve_agent_backend

    monkeypatch.delenv("ARC_AGENT_BACKEND", raising=False)
    monkeypatch.delenv("ARC_OCTOS_LOCAL", raising=False)
    binary = tmp_path / "octos"
    binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    binary.chmod(0o755)
    output_dir = tmp_path / "output"

    explicit = build_parser().parse_args(
        [
            "compile",
            str(tmp_path),
            "-o",
            str(output_dir),
            "--agent-backend",
            "octos-local",
            "--octos-bin",
            str(binary),
        ]
    )
    explicit_backend = resolve_agent_backend(explicit, workspace_root=str(output_dir))
    assert isinstance(explicit_backend, LocalOctosBackend)
    assert explicit_backend.delegator.workspace_root == str(output_dir.resolve())

    legacy = build_parser().parse_args(
        [
            "compile",
            str(tmp_path),
            "-o",
            str(output_dir),
            "--octos-local",
            "--octos-bin",
            str(binary),
        ]
    )
    legacy_backend = resolve_agent_backend(legacy, workspace_root=str(output_dir))
    assert isinstance(legacy_backend, LocalOctosBackend)

    monkeypatch.setenv("ARC_AGENT_BACKEND", "octos-local")
    monkeypatch.setenv("OCTOS_BIN", str(binary))
    configured = build_parser().parse_args(
        ["compile", str(tmp_path), "-o", str(output_dir)]
    )
    configured_backend = resolve_agent_backend(
        configured,
        workspace_root=str(output_dir),
    )
    assert isinstance(configured_backend, LocalOctosBackend)


@pytest.mark.parametrize(
    "delegation_args",
    [
        ["--octos-local"],
        ["--octos-mcp", "http://127.0.0.1:4033/mcp"],
        ["--agent-chat-url", "http://127.0.0.1:8090", "--delegate-to", "worker"],
    ],
)
def test_agent_backend_cli_rejects_builtin_with_delegation_flags(
    tmp_path,
    monkeypatch,
    delegation_args,
):
    from main import build_parser, resolve_agent_backend

    monkeypatch.delenv("ARC_AGENT_BACKEND", raising=False)
    monkeypatch.delenv("ARC_OCTOS_LOCAL", raising=False)
    args = build_parser().parse_args(
        [
            "compile",
            str(tmp_path),
            "-o",
            str(tmp_path / "output"),
            "--agent-backend",
            "builtin",
            *delegation_args,
        ]
    )

    with pytest.raises(SystemExit, match="builtin"):
        resolve_agent_backend(args, workspace_root=str(tmp_path / "output"))


async def test_design_adapters_submit_compiled_tasks_to_backend(quiet_context):
    from agents.interface_designer import InterfaceDesigner
    from agents.test_generator import TestGenerator

    backend = RecordingBackend(
        {
            "InterfaceDesigner": {
                "summary": "designed",
                "interfaces": [{"interface_id": "API-1", "type": "API"}],
            },
            "TestGenerator": {
                "summary": "tested",
                "tests": [
                    {
                        "test_id": "TEST-1",
                        "req_id": "REQ-3",
                        "interface_ids": ["API-1"],
                        "type": "Unit",
                        "file_path": "tests/test_api.py",
                        "first_line": "def test_api():",
                    }
                ],
            },
        }
    )
    requirement = {"id": "REQ-3", "title": "API", "children_ids": []}
    designer = InterfaceDesigner(
        workspace_root=str(quiet_context),
        app_type="cli",
        agent_backend=backend,
    )
    generator = TestGenerator(
        workspace_root=str(quiet_context),
        app_type="cli",
        agent_backend=backend,
    )

    design = await designer.run(node_id="REQ-3", requirement_data=requirement)
    tests, _ = await generator.run("REQ-3", requirement)

    assert design["interfaces"][0]["interface_id"] == "API-1"
    assert tests[0]["test_id"] == "TEST-1"
    assert [task.stage for task in backend.tasks] == ["InterfaceDesigner", "TestGenerator"]
    design_task, test_task = backend.tasks
    assert design_task.inputs["requirement"] == requirement
    assert design_task.response_schema["type"] == "object"
    assert test_task.inputs["requirement"] == requirement
    assert test_task.inputs["interface_contract"] == "interface contract for REQ-3"
    assert test_task.response_schema["type"] == "object"


async def test_external_backend_tdd_claim_requires_system_test_pass(quiet_context):
    from agents.test_driven_developer import TestDrivenDeveloper

    backend = RecordingBackend({"TestDrivenDeveloper": {"summary": "implemented"}})

    async def failing_tests(test_type, test_files):
        return "Exit Code: 1\nSTDERR:\nexpected 200, got 500"

    developer = TestDrivenDeveloper(
        workspace_root=str(quiet_context),
        app_type="cli",
        agent_backend=backend,
    )
    result = await developer.run(
        node_id="REQ-4",
        test_files=["tests/test_api.py"],
        test_type="Unit",
        node_tests=[
            {
                "test_id": "TEST-2",
                "type": "Unit",
                "file_path": "tests/test_api.py",
            }
        ],
        run_tests_executor=failing_tests,
    )

    assert "IMPLEMENTED" not in result.upper()
    assert "Exit Code: 1" in result
    assert backend.tasks[0].inputs["test_type"] == "Unit"
    assert backend.tasks[0].acceptance["system_test_verification"] is True


async def test_local_octos_backend_executes_same_compiled_task_contract(tmp_path):
    from agents.backend import CompiledAgentTask, LocalOctosBackend

    class FakeDelegator:
        def __init__(self):
            self.calls = []
            self.close_calls = 0

        async def invoke_stage(self, **kwargs):
            self.calls.append(kwargs)
            return {"summary": "octos completed"}

        async def aclose(self):
            self.close_calls += 1

    delegator = FakeDelegator()
    backend = LocalOctosBackend(delegator)
    task = CompiledAgentTask(
        task_id="REQ-5:DESIGN:InterfaceDesigner",
        stage="InterfaceDesigner",
        backend_agent_name="interface_designer",
        node_id="REQ-5",
        phase="DESIGN",
        app_type="web",
        workspace_root=str(tmp_path),
        requirement_path=str(tmp_path / "requirements.yaml"),
        system_prompt="system",
        message="task",
        response_schema={"type": "object"},
        inputs={"requirement": {"id": "REQ-5"}},
        acceptance={"response_schema_required": True},
    )

    output = await backend.execute(task)
    await backend.aclose()

    assert output == {"summary": "octos completed"}
    saved = list((tmp_path / ".arc" / "agent_tasks").glob("*.json"))
    assert len(saved) == 1
    assert delegator.calls == [
        {
            "stage": "InterfaceDesigner",
            "node_id": "REQ-5",
            "phase": "DESIGN",
            "workspace_root": str(tmp_path),
            "system_prompt": "system",
            "message": "task",
            "response_schema": {"type": "object"},
            "compiled_task": task.to_payload(),
        }
    ]
    assert delegator.calls[0]["compiled_task"]["schema"] == "arc.agent-task.v1"
    assert delegator.close_calls == 1


async def test_compile_closes_injected_agent_backend_once(tmp_path, monkeypatch):
    import main as main_mod

    requirement_dir = tmp_path / "requirements"
    requirement_dir.mkdir()
    (requirement_dir / "requirements.yaml").write_text(
        "id: ROOT\ntitle: Root\nchildren: []\n",
        encoding="utf-8",
    )
    backend = RecordingBackend()

    class FakeWorkflow:
        def __init__(self, **kwargs):
            assert kwargs["agent_backend"] is backend

        async def start_compilation(self, **kwargs):
            return {"ok": True, "failed_nodes": []}

    monkeypatch.setattr(main_mod, "resolve_agent_backend", lambda *a, **k: backend)
    monkeypatch.setattr(main_mod, "ARCWorkflowManager", FakeWorkflow)
    monkeypatch.setattr(main_mod, "print_cli_banner", lambda: None)
    monkeypatch.setattr(main_mod, "print_cli_startup", lambda **kwargs: None)
    monkeypatch.setattr(main_mod, "print_compilation_summary", lambda *args: None)
    monkeypatch.setattr(main_mod, "stop_cli_spinner", lambda: None)
    monkeypatch.setattr(
        main_mod,
        "init_debug_logger",
        lambda *args, **kwargs: str(tmp_path / "debug.log"),
    )

    args = main_mod.build_parser().parse_args(
        [
            "compile",
            str(requirement_dir),
            "-o",
            str(tmp_path / "output"),
            "--agent-backend",
            "builtin",
        ]
    )
    result = await main_mod.cmd_compile(args)

    assert result == 0
    assert backend.close_calls == 1
