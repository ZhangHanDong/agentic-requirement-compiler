from __future__ import annotations

from integrations.agent_chat_worker import AgentChatWorker


def task_request_message(msg_id="msg_1", sender="wf_coordinator", payload=None):
    if payload is None:
        payload = {"requirement_dir": "/tmp/req", "app_type": "web"}
    return {
        "id": msg_id,
        "from": sender,
        "type": "request",
        "summary": "compile this",
        "full": "compile the ticketbooking demo",
        "schema": {"kind": "task_request", "version": 1, "payload": payload},
    }


def make_worker(chat_server, task_runner, **overrides):
    kwargs = {
        "base_url": chat_server.base_url,
        "agent_name": "arc-compiler",
        "api_token": "test-token",
        "task_runner": task_runner,
    }
    kwargs.update(overrides)
    return AgentChatWorker(**kwargs)


async def noop_runner(payload):
    return {"ok": True}


async def test_heartbeat_posts_and_marks_online(chat_server):
    worker = make_worker(chat_server, noop_runner)
    try:
        assert await worker.heartbeat()
    finally:
        await worker.aclose()

    request = chat_server.requests[0]
    assert request["path"] == "/api/agents/arc-compiler/heartbeat"
    assert request["headers"].get("Authorization") == "Bearer test-token"


async def test_run_once_executes_task_request_and_replies(chat_server):
    chat_server.get_responses["/api/inbox/arc-compiler"] = {
        "dm": [task_request_message()],
        "group": [],
    }
    seen_payloads = []

    async def runner(payload):
        seen_payloads.append(payload)
        return {"ok": True, "failed_nodes": [], "output_dir": "/tmp/out"}

    worker = make_worker(chat_server, runner)
    try:
        handled = await worker.run_once()
    finally:
        await worker.aclose()

    assert handled == 1
    assert seen_payloads == [{"requirement_dir": "/tmp/req", "app_type": "web"}]
    replies = [r for r in chat_server.requests if r["path"] == "/api/messages"]
    assert len(replies) == 1
    body = replies[0]["body"]
    assert body["from"] == "arc-compiler"
    assert body["to"] == "wf_coordinator"
    assert body["type"] == "reply"
    assert body["reply_to"] == "msg_1"
    assert body["schema"]["kind"] == "task_result"
    result = body["schema"]["payload"]
    assert result["ok"] is True
    assert result["output_dir"] == "/tmp/out"


async def test_run_once_ignores_non_task_messages(chat_server):
    chat_server.get_responses["/api/inbox/arc-compiler"] = {
        "dm": [
            {"id": "msg_2", "from": "someone", "type": "inform", "summary": "hi", "schema": None},
        ],
        "group": [{"id": "msg_3", "from": "other", "group": "arc", "summary": "chatter"}],
    }
    calls = []

    async def runner(payload):
        calls.append(payload)
        return {}

    worker = make_worker(chat_server, runner)
    try:
        handled = await worker.run_once()
    finally:
        await worker.aclose()

    assert handled == 0
    assert calls == []
    assert [r for r in chat_server.requests if r["path"] == "/api/messages"] == []


async def test_runner_failure_replies_with_error(chat_server):
    chat_server.get_responses["/api/inbox/arc-compiler"] = {
        "dm": [task_request_message(msg_id="msg_4")],
        "group": [],
    }

    async def runner(payload):
        raise RuntimeError("compile exploded")

    worker = make_worker(chat_server, runner)
    try:
        handled = await worker.run_once()
    finally:
        await worker.aclose()

    assert handled == 1
    body = [r for r in chat_server.requests if r["path"] == "/api/messages"][0]["body"]
    assert body["priority"] == "high"
    result = body["schema"]["payload"]
    assert result["ok"] is False
    assert "compile exploded" in result["error"]


async def test_missing_requirement_dir_replies_error_without_running(chat_server):
    chat_server.get_responses["/api/inbox/arc-compiler"] = {
        "dm": [task_request_message(msg_id="msg_5", payload={"app_type": "web"})],
        "group": [],
    }
    calls = []

    async def runner(payload):
        calls.append(payload)
        return {}

    worker = make_worker(chat_server, runner)
    try:
        await worker.run_once()
    finally:
        await worker.aclose()

    assert calls == []
    body = [r for r in chat_server.requests if r["path"] == "/api/messages"][0]["body"]
    result = body["schema"]["payload"]
    assert result["ok"] is False
    assert "requirement_dir" in result["error"]


async def test_run_once_survives_backend_being_down():
    async def runner(payload):
        raise AssertionError("should not run")

    worker = AgentChatWorker(
        base_url="http://127.0.0.1:1",
        agent_name="arc-compiler",
        task_runner=runner,
    )
    try:
        handled = await worker.run_once()
        assert handled == 0
        assert not await worker.heartbeat()
    finally:
        await worker.aclose()


async def test_agent_token_header_sent_when_configured(chat_server):
    worker = make_worker(chat_server, noop_runner, agent_token="agent-secret")
    try:
        await worker.heartbeat()
    finally:
        await worker.aclose()

    headers = chat_server.requests[0]["headers"]
    assert headers.get("X-Agent-Token") == "agent-secret"


async def test_run_forever_heartbeats_polls_and_stops(chat_server):
    import asyncio

    chat_server.get_responses["/api/inbox/arc-compiler"] = {
        "dm": [task_request_message(msg_id="msg_loop")],
        "group": [],
    }
    stop = asyncio.Event()

    async def runner(payload):
        stop.set()
        return {"ok": True}

    worker = make_worker(chat_server, runner, poll_interval=0.01, heartbeat_interval=0.01)
    try:
        await asyncio.wait_for(worker.run_forever(stop), timeout=5)
    finally:
        await worker.aclose()

    paths = [r["path"].split("?")[0] for r in chat_server.requests]
    assert "/api/agents/arc-compiler/heartbeat" in paths
    assert "/api/inbox/arc-compiler" in paths
    assert "/api/messages" in paths
