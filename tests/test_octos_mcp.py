from __future__ import annotations

import json
import sys
import textwrap
from pathlib import Path

import pytest

from integrations.octos_mcp import (
    LocalOctosMcpDelegator,
    OctosMcpDelegator,
    build_octos_delegator,
)
from integrations.stage_delegation import StageDelegationError
from main import build_parser, resolve_stage_delegator


def octos_envelope(body: dict, *, is_error: bool = False, rpc_error: dict | None = None):
    if rpc_error is not None:
        return {"jsonrpc": "2.0", "id": 1, "error": rpc_error}
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {
            "content": [{"type": "text", "text": json.dumps(body)}],
            "isError": is_error,
        },
    }


def make_delegator(chat_server, **overrides):
    kwargs = {
        "base_url": chat_server.base_url,
        "token": "octos-token",
        "endpoint": "/mcp",
        "timeout": 5.0,
    }
    kwargs.update(overrides)
    return OctosMcpDelegator(**kwargs)


def write_fake_octos_server(tmp_path: Path, *, mode: str = "ready") -> tuple[str, ...]:
    """Return an argv prefix for a line-delimited MCP stdio test server."""
    script = tmp_path / f"fake_octos_{mode}.py"
    log_path = tmp_path / f"fake_octos_{mode}.jsonl"
    script.write_text(
        textwrap.dedent(
            """
            import json
            import pathlib
            import sys
            import time

            mode = sys.argv[1]
            log_path = pathlib.Path(sys.argv[2])
            calls = 0

            def record(payload):
                with log_path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(payload) + "\\n")

            def respond(request_id, result=None, error=None):
                envelope = {"jsonrpc": "2.0", "id": request_id}
                if error is not None:
                    envelope["error"] = error
                else:
                    envelope["result"] = result
                print(json.dumps(envelope), flush=True)

            record({"argv": sys.argv[3:]})
            for raw in sys.stdin:
                request = json.loads(raw)
                record(request)
                method = request.get("method")
                if method == "notifications/initialized":
                    continue
                if mode == "timeout":
                    time.sleep(60)
                    continue
                if method == "initialize":
                    respond(
                        request["id"],
                        {
                            "protocolVersion": "2024-11-05",
                            "capabilities": {"tools": {"listChanged": False}},
                            "serverInfo": {"name": "fake-octos", "version": "test"},
                        },
                    )
                elif method == "tools/list":
                    tools = [] if mode == "missing-tool" else [{"name": "run_octos_session"}]
                    respond(request["id"], {"tools": tools})
                elif method == "tools/call":
                    calls += 1
                    if mode == "tool-error":
                        body = {
                            "final_state": "failed",
                            "error": "contract_failed: fake validator failed",
                        }
                        respond(
                            request["id"],
                            {
                                "content": [{"type": "text", "text": json.dumps(body)}],
                                "isError": True,
                            },
                        )
                    else:
                        body = {
                            "final_state": "ready",
                            "contract": request["params"]["arguments"]["contract"],
                            "artifact_content": json.dumps({"summary": f"call-{calls}"}),
                            "validator_results": [],
                            "cost": {},
                        }
                        respond(
                            request["id"],
                            {
                                "content": [{"type": "text", "text": json.dumps(body)}],
                                "isError": False,
                            },
                        )
            """
        ),
        encoding="utf-8",
    )
    return sys.executable, str(script), mode, str(log_path)


def read_fake_octos_log(command: tuple[str, ...]) -> list[dict]:
    log_path = Path(command[3])
    return [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]


def stage_request(*, stage: str = "InterfaceDesigner", node_id: str = "n1") -> dict:
    return {
        "stage": stage,
        "node_id": node_id,
        "phase": "DESIGN",
        "workspace_root": "/tmp/ws",
        "system_prompt": "system role",
        "message": "stage task",
        "response_schema": {"type": "object"},
    }


def test_local_octos_cli_builds_stdio_delegator_for_output_workspace(tmp_path, monkeypatch):
    monkeypatch.delenv("ARC_OCTOS_LOCAL", raising=False)
    monkeypatch.delenv("OCTOS_BIN", raising=False)
    binary = tmp_path / "octos"
    binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    binary.chmod(0o755)
    output_dir = tmp_path / "output"
    args = build_parser().parse_args(
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

    delegator = resolve_stage_delegator(args, workspace_root=str(output_dir))

    assert isinstance(delegator, LocalOctosMcpDelegator)
    assert delegator.workspace_root == str(output_dir.resolve())
    assert delegator.process_argv == (
        str(binary),
        "mcp-serve",
        "--transport",
        "stdio",
        "--cwd",
        str(output_dir.resolve()),
    )

    implied_args = build_parser().parse_args(
        [
            "compile",
            str(tmp_path),
            "-o",
            str(output_dir),
            "--octos-bin",
            str(binary),
        ]
    )
    assert isinstance(
        resolve_stage_delegator(implied_args, workspace_root=str(output_dir)),
        LocalOctosMcpDelegator,
    )

    monkeypatch.setenv("ARC_OCTOS_LOCAL", "1")
    monkeypatch.setenv("OCTOS_BIN", str(binary))
    env_args = build_parser().parse_args(
        ["compile", str(tmp_path), "-o", str(output_dir)]
    )
    assert isinstance(
        resolve_stage_delegator(env_args, workspace_root=str(output_dir)),
        LocalOctosMcpDelegator,
    )


@pytest.mark.parametrize(
    "extra_args",
    [
        ["--octos-mcp", "http://127.0.0.1:4033/mcp"],
        ["--agent-chat-url", "http://127.0.0.1:8090", "--delegate-to", "worker"],
    ],
)
def test_local_octos_cli_rejects_multiple_delegation_backends(
    tmp_path,
    monkeypatch,
    extra_args,
):
    monkeypatch.delenv("ARC_OCTOS_LOCAL", raising=False)
    binary = tmp_path / "octos"
    binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    binary.chmod(0o755)
    args = build_parser().parse_args(
        [
            "compile",
            str(tmp_path),
            "-o",
            str(tmp_path / "output"),
            "--octos-local",
            "--octos-bin",
            str(binary),
            *extra_args,
        ]
    )

    with pytest.raises(SystemExit, match="mutually exclusive"):
        resolve_stage_delegator(args, workspace_root=str(tmp_path / "output"))


def test_local_octos_cli_rejects_missing_binary(tmp_path, monkeypatch):
    monkeypatch.delenv("ARC_OCTOS_LOCAL", raising=False)
    missing = tmp_path / "missing-octos"
    args = build_parser().parse_args(
        [
            "compile",
            str(tmp_path),
            "-o",
            str(tmp_path / "output"),
            "--octos-local",
            "--octos-bin",
            str(missing),
        ]
    )

    with pytest.raises(SystemExit, match=str(missing)):
        resolve_stage_delegator(args, workspace_root=str(tmp_path / "output"))


async def test_local_octos_stdio_delegates_stage_and_reuses_process(tmp_path):
    command = write_fake_octos_server(tmp_path)
    delegator = LocalOctosMcpDelegator(
        command,
        workspace_root=str(tmp_path),
        timeout=2.0,
        startup_timeout=2.0,
    )
    try:
        first = await delegator.invoke_stage(**stage_request())
        second = await delegator.invoke_stage(
            **stage_request(stage="TestGenerator", node_id="n2")
        )
    finally:
        await delegator.aclose()

    assert first == {"summary": "call-1"}
    assert second == {"summary": "call-2"}
    records = read_fake_octos_log(command)
    assert records[0]["argv"] == [
        "mcp-serve",
        "--transport",
        "stdio",
        "--cwd",
        str(tmp_path.resolve()),
    ]
    methods = [record.get("method") for record in records[1:]]
    assert methods == [
        "initialize",
        "notifications/initialized",
        "tools/list",
        "tools/call",
        "tools/call",
    ]
    tool_calls = [record for record in records if record.get("method") == "tools/call"]
    arguments = tool_calls[0]["params"]["arguments"]
    assert arguments["contract"] == "coding"
    assert arguments["input"]["artifact_name"] == "primary"
    assert arguments["input"]["expected_artifact"] == ".arc/delegated/InterfaceDesigner-n1.json"
    assert "system role" in arguments["input"]["prompt"]


async def test_local_octos_stdio_rejects_server_without_run_session_tool(tmp_path):
    command = write_fake_octos_server(tmp_path, mode="missing-tool")
    delegator = LocalOctosMcpDelegator(
        command,
        workspace_root=str(tmp_path),
        timeout=2.0,
        startup_timeout=2.0,
    )
    try:
        with pytest.raises(StageDelegationError, match="run_octos_session"):
            await delegator.invoke_stage(**stage_request())
    finally:
        await delegator.aclose()

    methods = [record.get("method") for record in read_fake_octos_log(command)[1:]]
    assert "tools/call" not in methods


async def test_local_octos_stdio_surfaces_tool_error(tmp_path):
    command = write_fake_octos_server(tmp_path, mode="tool-error")
    delegator = LocalOctosMcpDelegator(
        command,
        workspace_root=str(tmp_path),
        timeout=2.0,
        startup_timeout=2.0,
    )
    try:
        with pytest.raises(StageDelegationError, match="contract_failed"):
            await delegator.invoke_stage(**stage_request())
    finally:
        await delegator.aclose()


async def test_local_octos_stdio_timeout_stops_child_process(tmp_path):
    command = write_fake_octos_server(tmp_path, mode="timeout")
    delegator = LocalOctosMcpDelegator(
        command,
        workspace_root=str(tmp_path),
        timeout=0.05,
        startup_timeout=0.05,
    )

    with pytest.raises(StageDelegationError, match="timed out"):
        await delegator.invoke_stage(**stage_request())

    assert not delegator.is_running


async def test_local_octos_stdio_close_ends_child_process(tmp_path):
    command = write_fake_octos_server(tmp_path)
    delegator = LocalOctosMcpDelegator(
        command,
        workspace_root=str(tmp_path),
        timeout=2.0,
        startup_timeout=2.0,
    )

    await delegator.start()
    assert delegator.is_running
    await delegator.aclose()

    assert not delegator.is_running


async def test_invoke_stage_sends_tools_call_and_parses_artifact(chat_server):
    output = {"summary": "designed", "interfaces": [{"name": "POST /api/x"}], "files_written": []}
    chat_server.post_responses["/mcp"] = octos_envelope(
        {
            "final_state": "ready",
            "contract": "coding",
            "artifact_path": "/tmp/ws/.arc/delegated/InterfaceDesigner-n1.json",
            "artifact_content": json.dumps(output),
            "validator_results": [],
            "cost": {"input_tokens": 10},
        }
    )

    delegator = make_delegator(chat_server)
    try:
        result = await delegator.invoke_stage(
            stage="InterfaceDesigner",
            node_id="n1",
            phase="DESIGN",
            workspace_root="/tmp/ws",
            system_prompt="You are the interface designer.",
            message="Design interfaces for n1.",
            response_schema={"type": "object"},
        )
    finally:
        await delegator.aclose()

    assert result == output

    sent = chat_server.requests[0]
    assert sent["path"] == "/mcp"
    assert sent["headers"].get("Authorization") == "Bearer octos-token"
    body = sent["body"]
    assert body["jsonrpc"] == "2.0"
    assert body["method"] == "tools/call"
    assert body["params"]["name"] == "run_octos_session"
    args = body["params"]["arguments"]
    assert args["contract"] == "coding"
    task_input = args["input"]
    assert task_input["expected_artifact"] == ".arc/delegated/InterfaceDesigner-n1.json"
    assert "You are the interface designer." in task_input["prompt"]
    assert "Design interfaces for n1." in task_input["prompt"]
    assert ".arc/delegated/InterfaceDesigner-n1.json" in task_input["prompt"]


async def test_tdd_non_json_artifact_wrapped_as_summary(chat_server):
    chat_server.post_responses["/mcp"] = octos_envelope(
        {
            "final_state": "ready",
            "artifact_path": "/tmp/ws/.arc/delegated/TestDrivenDeveloper-n2.json",
            "artifact_content": "IMPLEMENTED the endpoint",
            "validator_results": [],
            "cost": {},
        }
    )

    delegator = make_delegator(chat_server)
    try:
        result = await delegator.invoke_stage(
            stage="TestDrivenDeveloper",
            node_id="n2",
            phase="IMPLEMENT",
            workspace_root="/tmp/ws",
            system_prompt="sys",
            message="msg",
            response_schema=None,
        )
    finally:
        await delegator.aclose()

    assert result == {"summary": "IMPLEMENTED the endpoint"}


async def test_typed_error_from_octos_raises(chat_server):
    chat_server.post_responses["/mcp"] = octos_envelope(
        {
            "final_state": "failed",
            "validator_results": [],
            "cost": {},
            "error": "contract_failed: required completion-phase validator failed",
        },
        is_error=True,
    )

    delegator = make_delegator(chat_server)
    try:
        with pytest.raises(StageDelegationError, match="contract_failed"):
            await delegator.invoke_stage(
                stage="TestGenerator",
                node_id="n3",
                phase="DESIGN",
                workspace_root="/tmp/ws",
                system_prompt="sys",
                message="msg",
            )
    finally:
        await delegator.aclose()


async def test_json_rpc_error_envelope_raises(chat_server):
    chat_server.post_responses["/mcp"] = octos_envelope(
        {}, rpc_error={"code": -32001, "message": "authentication required"}
    )

    delegator = make_delegator(chat_server)
    try:
        with pytest.raises(StageDelegationError, match="authentication required"):
            await delegator.invoke_stage(
                stage="InterfaceDesigner",
                node_id="n4",
                phase="DESIGN",
                workspace_root="/tmp/ws",
                system_prompt="sys",
                message="msg",
            )
    finally:
        await delegator.aclose()


async def test_http_failure_raises(chat_server):
    chat_server.response_status = 500

    delegator = make_delegator(chat_server)
    try:
        with pytest.raises(StageDelegationError):
            await delegator.invoke_stage(
                stage="InterfaceDesigner",
                node_id="n5",
                phase="DESIGN",
                workspace_root="/tmp/ws",
                system_prompt="sys",
                message="msg",
            )
    finally:
        await delegator.aclose()


async def test_missing_artifact_content_on_success_raises(chat_server):
    chat_server.post_responses["/mcp"] = octos_envelope(
        {
            "final_state": "ready",
            "artifact_path": "/tmp/ws/out.json",
            "validator_results": [],
            "cost": {},
        }
    )

    delegator = make_delegator(chat_server)
    try:
        with pytest.raises(StageDelegationError, match="artifact"):
            await delegator.invoke_stage(
                stage="InterfaceDesigner",
                node_id="n6",
                phase="DESIGN",
                workspace_root="/tmp/ws",
                system_prompt="sys",
                message="msg",
                response_schema={"type": "object"},
            )
    finally:
        await delegator.aclose()


async def test_build_octos_delegator_from_args_and_env():
    assert build_octos_delegator(url=None, env={}) is None

    delegator = build_octos_delegator(
        url="http://127.0.0.1:4033/mcp",
        env={"OCTOS_MCP_SERVER_TOKEN": "tok", "OCTOS_MCP_CONTRACT": "site_delivery"},
    )
    assert delegator is not None
    try:
        assert delegator.contract == "site_delivery"
    finally:
        await delegator.aclose()

    env_only = build_octos_delegator(env={"OCTOS_MCP_URL": "http://127.0.0.1:4033/mcp"})
    assert env_only is not None
    try:
        assert env_only.contract == "coding"
    finally:
        await env_only.aclose()


async def test_octos_delegator_drives_interface_designer_adapter(monkeypatch, tmp_path, chat_server):
    """End-to-end: an ARC stage adapter executes through the Octos backend."""
    from agents.backend import DelegatingAgentBackend
    import agents.interface_designer as mod
    from context.context_pipeline import context_pipeline

    monkeypatch.setattr(context_pipeline, "build_agent_context_split", lambda **kw: ("", ""))

    output = {"summary": "octos designed", "interfaces": [{"name": "GET /api/t"}], "files_written": []}
    chat_server.post_responses["/mcp"] = octos_envelope(
        {
            "final_state": "ready",
            "artifact_content": json.dumps(output),
            "validator_results": [],
            "cost": {},
        }
    )

    delegator = make_delegator(chat_server)
    backend = DelegatingAgentBackend(delegator, name="octos-http")
    try:
        designer = mod.InterfaceDesigner(
            workspace_root=str(tmp_path),
            agent_backend=backend,
        )
        bundle = await designer.run(node_id="n-octos", requirement_data={"children_ids": []})
    finally:
        await backend.aclose()

    assert bundle["summary"] == "octos designed"
    assert bundle["interfaces"] == [{"name": "GET /api/t"}]
