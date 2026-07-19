from __future__ import annotations

import pytest

from integrations.stage_delegation import (
    StageDelegationError,
    StageDelegator,
    get_stage_delegator,
    set_stage_delegator,
)


def make_delegator(chat_server, **overrides):
    kwargs = {
        "base_url": chat_server.base_url,
        "implementer": "claude-implementer",
        "agent_name": "arc-compiler",
        "api_token": "test-token",
        "poll_interval": 0.01,
        "timeout": 5.0,
    }
    kwargs.update(overrides)
    return StageDelegator(**kwargs)


def stage_reply(reply_to, *, ok=True, output=None, error=None):
    payload = {"ok": ok}
    if output is not None:
        payload["output"] = output
    if error is not None:
        payload["error"] = error
    return {
        "id": "msg_reply_1",
        "from": "claude-implementer",
        "type": "reply",
        "reply_to": reply_to,
        "summary": "done",
        "schema": {"kind": "task_result", "version": 1, "payload": payload},
    }


async def test_invoke_stage_sends_task_and_returns_output(chat_server):
    chat_server.post_responses["/api/messages"] = {"ok": True, "id": "msg_sent_1"}
    chat_server.get_responses["/api/inbox/arc-compiler"] = {
        "dm": [stage_reply("msg_sent_1", output={"summary": "designed", "interfaces": [{"name": "POST /api/x"}]})],
        "group": [],
    }

    delegator = make_delegator(chat_server)
    try:
        output = await delegator.invoke_stage(
            stage="InterfaceDesigner",
            node_id="node-1",
            phase="DESIGN",
            workspace_root="/tmp/ws",
            system_prompt="You are the interface designer.",
            message="Design interfaces for node-1.",
            response_schema={"type": "object"},
        )
    finally:
        await delegator.aclose()

    assert output == {"summary": "designed", "interfaces": [{"name": "POST /api/x"}]}

    sends = [r for r in chat_server.requests if r["path"].startswith("/api/messages")]
    assert len(sends) == 1
    body = sends[0]["body"]
    assert body["from"] == "arc-compiler"
    assert body["to"] == "claude-implementer"
    assert body["type"] == "request"
    assert body["schema"]["kind"] == "task_request"
    task = body["schema"]["payload"]
    assert task["protocol"] == "arc_stage"
    assert task["stage"] == "InterfaceDesigner"
    assert task["node_id"] == "node-1"
    assert task["workspace"] == "/tmp/ws"
    assert "You are the interface designer." in body["full"]
    assert "Design interfaces for node-1." in body["full"]
    assert "task_result" in body["full"]

    polls = [r for r in chat_server.requests if r["path"].startswith("/api/inbox/arc-compiler")]
    assert polls and "kinds=task_result" in polls[0]["path"]


async def test_invoke_stage_ignores_unrelated_replies_until_match(chat_server):
    chat_server.post_responses["/api/messages"] = {"ok": True, "id": "msg_sent_2"}
    chat_server.get_responses["/api/inbox/arc-compiler"] = {
        "dm": [
            stage_reply("some_other_msg", output={"summary": "stale"}),
            stage_reply("msg_sent_2", output={"summary": "fresh"}),
        ],
        "group": [],
    }

    delegator = make_delegator(chat_server)
    try:
        output = await delegator.invoke_stage(
            stage="TestGenerator",
            node_id="node-2",
            phase="DESIGN",
            workspace_root="/tmp/ws",
            system_prompt="sys",
            message="msg",
        )
    finally:
        await delegator.aclose()

    assert output["summary"] == "fresh"


async def test_invoke_stage_times_out(chat_server):
    chat_server.post_responses["/api/messages"] = {"ok": True, "id": "msg_sent_3"}
    chat_server.get_responses["/api/inbox/arc-compiler"] = {"dm": [], "group": []}

    delegator = make_delegator(chat_server, timeout=0.05)
    try:
        with pytest.raises(StageDelegationError, match="timed out"):
            await delegator.invoke_stage(
                stage="InterfaceDesigner",
                node_id="node-3",
                phase="DESIGN",
                workspace_root="/tmp/ws",
                system_prompt="sys",
                message="msg",
            )
    finally:
        await delegator.aclose()


async def test_invoke_stage_raises_on_failed_reply(chat_server):
    chat_server.post_responses["/api/messages"] = {"ok": True, "id": "msg_sent_4"}
    chat_server.get_responses["/api/inbox/arc-compiler"] = {
        "dm": [stage_reply("msg_sent_4", ok=False, error="implementer crashed")],
        "group": [],
    }

    delegator = make_delegator(chat_server)
    try:
        with pytest.raises(StageDelegationError, match="implementer crashed"):
            await delegator.invoke_stage(
                stage="TestDrivenDeveloper",
                node_id="node-4",
                phase="IMPLEMENT",
                workspace_root="/tmp/ws",
                system_prompt="sys",
                message="msg",
            )
    finally:
        await delegator.aclose()


async def test_invoke_stage_raises_when_send_fails(chat_server):
    chat_server.response_status = 500

    delegator = make_delegator(chat_server)
    try:
        with pytest.raises(StageDelegationError, match="send"):
            await delegator.invoke_stage(
                stage="InterfaceDesigner",
                node_id="node-5",
                phase="DESIGN",
                workspace_root="/tmp/ws",
                system_prompt="sys",
                message="msg",
            )
    finally:
        await delegator.aclose()


async def test_non_dict_output_is_wrapped_as_summary(chat_server):
    chat_server.post_responses["/api/messages"] = {"ok": True, "id": "msg_sent_6"}
    chat_server.get_responses["/api/inbox/arc-compiler"] = {
        "dm": [stage_reply("msg_sent_6", output="IMPLEMENTED all tests pass")],
        "group": [],
    }

    delegator = make_delegator(chat_server)
    try:
        output = await delegator.invoke_stage(
            stage="TestDrivenDeveloper",
            node_id="node-6",
            phase="IMPLEMENT",
            workspace_root="/tmp/ws",
            system_prompt="sys",
            message="msg",
        )
    finally:
        await delegator.aclose()

    assert output == {"summary": "IMPLEMENTED all tests pass"}


def test_singleton_set_and_get():
    assert get_stage_delegator() is None
    sentinel = object()
    set_stage_delegator(sentinel)
    try:
        assert get_stage_delegator() is sentinel
    finally:
        set_stage_delegator(None)
    assert get_stage_delegator() is None


async def test_build_stage_delegator_from_args_and_env():
    from integrations.stage_delegation import build_stage_delegator

    assert build_stage_delegator(url=None, implementer=None, env={}) is None
    assert build_stage_delegator(url="http://x:8090", implementer=None, env={}) is None

    delegator = build_stage_delegator(
        url="http://cli:8090",
        implementer="claude-implementer",
        env={
            "AGENT_CHAT_TOKEN": "tok",
            "AGENT_CHAT_AGENT_NAME": "arc-01",
            "ARC_DELEGATE_TIMEOUT": "120",
            "ARC_DELEGATE_POLL_INTERVAL": "1.5",
        },
    )
    assert delegator is not None
    try:
        assert delegator.implementer == "claude-implementer"
        assert delegator.agent_name == "arc-01"
        assert delegator.timeout == 120.0
        assert delegator.poll_interval == 1.5
    finally:
        await delegator.aclose()

    env_only = build_stage_delegator(
        env={"AGENT_CHAT_URL": "http://env:8090", "ARC_DELEGATE_TO": "codex-implementer"}
    )
    assert env_only is not None
    try:
        assert env_only.implementer == "codex-implementer"
    finally:
        await env_only.aclose()
