from __future__ import annotations

import pytest

from integrations.agent_chat import AgentChatReporter, build_reporter


async def test_returns_none_when_no_url_anywhere():
    assert build_reporter(url=None, env={}) is None


async def test_cli_args_take_precedence_over_env():
    reporter = build_reporter(
        url="http://cli:8090",
        group="arc-cli",
        run_label="run-1",
        env={
            "AGENT_CHAT_URL": "http://env:8090",
            "AGENT_CHAT_GROUP": "arc-env",
            "AGENT_CHAT_TOKEN": "secret",
        },
    )
    assert isinstance(reporter, AgentChatReporter)
    try:
        assert reporter.group == "arc-cli"
        assert reporter.run_label == "run-1"
    finally:
        await reporter.aclose()


async def test_env_only_configuration():
    reporter = build_reporter(
        env={
            "AGENT_CHAT_URL": "http://env:8090",
            "AGENT_CHAT_TO": "operator",
            "AGENT_CHAT_AGENT_NAME": "arc-01",
        }
    )
    assert isinstance(reporter, AgentChatReporter)
    try:
        assert reporter.to == "operator"
        assert reporter.group is None
        assert reporter.agent_name == "arc-01"
    finally:
        await reporter.aclose()


async def test_url_without_target_raises():
    with pytest.raises(ValueError, match="AGENT_CHAT_GROUP|AGENT_CHAT_TO|--agent-chat"):
        build_reporter(url="http://cli:8090", env={})
