from __future__ import annotations

import json

import pytest

from integrations.octos_mcp import OctosMcpDelegator, build_octos_delegator
from integrations.stage_delegation import StageDelegationError


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
    """End-to-end: an ARC stage adapter delegates to octos with zero adapter changes."""
    import agents.interface_designer as mod
    from context.context_pipeline import context_pipeline
    from integrations.stage_delegation import set_stage_delegator

    monkeypatch.setattr(context_pipeline, "build_agent_context_split", lambda **kw: ("", ""))

    def _fail(*a, **k):
        raise AssertionError("build_stage_agent must not be called in delegated mode")

    monkeypatch.setattr(mod, "build_stage_agent", _fail)

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
    set_stage_delegator(delegator)
    try:
        designer = mod.InterfaceDesigner(workspace_root=str(tmp_path))
        bundle = await designer.run(node_id="n-octos", requirement_data={"children_ids": []})
    finally:
        set_stage_delegator(None)
        await delegator.aclose()

    assert bundle["summary"] == "octos designed"
    assert bundle["interfaces"] == [{"name": "GET /api/t"}]
