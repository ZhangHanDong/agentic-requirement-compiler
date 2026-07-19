from __future__ import annotations

from integrations.agent_chat import AgentChatReporter


def make_reporter(chat_server, **overrides):
    kwargs = {
        "base_url": chat_server.base_url,
        "agent_name": "arc-compiler",
        "group": "arc",
        "api_token": "test-token",
        "run_label": "run-test",
    }
    kwargs.update(overrides)
    return AgentChatReporter(**kwargs)


async def test_forwards_error_status_message(chat_server):
    reporter = make_reporter(chat_server)
    cb = reporter.make_log_cb()
    try:
        await cb("TestGenerator", "generation failed", "error", "node-2")
    finally:
        await reporter.aclose()

    assert len(chat_server.requests) == 1
    request = chat_server.requests[0]
    assert request["path"] == "/api/messages"
    assert request["headers"].get("Authorization") == "Bearer test-token"
    body = request["body"]
    assert body["from"] == "arc-compiler"
    assert body["group"] == "arc"
    assert body["type"] == "inform"
    assert body["priority"] == "high"
    assert "generation failed" in body["full"]
    assert body["schema"]["kind"] == "arc_progress"
    payload = body["schema"]["payload"]
    assert payload["node_id"] == "node-2"
    assert payload["status"] == "error"
    assert payload["agent"] == "TestGenerator"
    assert payload["run"] == "run-test"


async def test_skips_unstatused_narration_from_stage_agents(chat_server):
    reporter = make_reporter(chat_server)
    cb = reporter.make_log_cb()
    try:
        await cb("InterfaceDesigner", "thinking about interfaces...", None, "node-1")
        await cb("TestDrivenDeveloper", "internal detail", "debug", "node-1")
    finally:
        await reporter.aclose()

    assert chat_server.requests == []


async def test_compiler_milestones_forwarded_without_status(chat_server):
    reporter = make_reporter(chat_server)
    cb = reporter.make_log_cb()
    try:
        await cb("Compiler", "ARC compilation started.", None, None)
    finally:
        await reporter.aclose()

    assert len(chat_server.requests) == 1
    body = chat_server.requests[0]["body"]
    assert body["priority"] == "normal"
    assert body["summary"].startswith("[Compiler]")


async def test_dm_mode_uses_to_instead_of_group(chat_server):
    reporter = make_reporter(chat_server, group=None, to="operator")
    cb = reporter.make_log_cb()
    try:
        await cb("Compiler", "done", "ok", None)
    finally:
        await reporter.aclose()

    body = chat_server.requests[0]["body"]
    assert body["to"] == "operator"
    assert "group" not in body


async def test_inner_callback_still_invoked(chat_server):
    seen: list[tuple] = []

    def inner(agent_name, message, status=None, node_id=None):
        seen.append((agent_name, message, status, node_id))

    reporter = make_reporter(chat_server)
    cb = reporter.make_log_cb(inner)
    try:
        await cb("InterfaceDesigner", "narration", None, "node-1")
        await cb("Compiler", "milestone", None, None)
    finally:
        await reporter.aclose()

    assert seen == [
        ("InterfaceDesigner", "narration", None, "node-1"),
        ("Compiler", "milestone", None, None),
    ]


async def test_disables_after_consecutive_failures(chat_server):
    chat_server.response_status = 500
    reporter = make_reporter(chat_server, max_consecutive_failures=2)
    cb = reporter.make_log_cb()
    try:
        await cb("Compiler", "one", None, None)
        await cb("Compiler", "two", None, None)
        await cb("Compiler", "three", None, None)
        await cb("Compiler", "four", None, None)
    finally:
        await reporter.aclose()

    assert len(chat_server.requests) == 2
    assert reporter.disabled


async def test_register_posts_agent(chat_server):
    reporter = make_reporter(chat_server)
    try:
        registered = await reporter.register()
    finally:
        await reporter.aclose()

    assert registered
    request = chat_server.requests[0]
    assert request["path"] == "/api/agents"
    assert request["body"]["name"] == "arc-compiler"


async def test_server_unreachable_never_raises():
    reporter = AgentChatReporter(
        base_url="http://127.0.0.1:1",
        agent_name="arc-compiler",
        group="arc",
        max_consecutive_failures=1,
    )
    cb = reporter.make_log_cb()
    try:
        await cb("Compiler", "milestone", None, None)
        assert not await reporter.register()
    finally:
        await reporter.aclose()

    assert reporter.disabled
