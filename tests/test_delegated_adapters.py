from __future__ import annotations

import pytest

from agents.backend import DelegatingAgentBackend


class FakeDelegator:
    def __init__(self, output):
        self.output = output
        self.calls: list[dict] = []

    async def invoke_stage(self, **kwargs):
        self.calls.append(kwargs)
        return self.output


@pytest.fixture
def quiet_context(monkeypatch, tmp_path):
    from context.context_pipeline import context_pipeline

    monkeypatch.setattr(
        context_pipeline, "build_agent_context_split", lambda **kwargs: ("", "")
    )
    monkeypatch.setattr(
        context_pipeline, "get_interface_contract_context", lambda node_id: ""
    )
    return tmp_path


async def test_interface_designer_uses_delegator(quiet_context):
    import agents.interface_designer as mod

    fake = FakeDelegator(
        {
            "summary": "designed",
            "interfaces": [{"name": "POST /api/x"}],
            "files_written": ["backend/src/x.ts"],
        }
    )
    backend = DelegatingAgentBackend(fake, name="test-delegator")

    designer = mod.InterfaceDesigner(
        workspace_root=str(quiet_context),
        agent_backend=backend,
    )
    bundle = await designer.run(node_id="n1", requirement_data={"children_ids": []})

    assert bundle["summary"] == "designed"
    assert bundle["interfaces"] == [{"name": "POST /api/x"}]
    assert bundle["files_written"] == ["backend/src/x.ts"]
    call = fake.calls[0]
    assert call["stage"] == "InterfaceDesigner"
    assert call["phase"] == "DESIGN"
    assert call["node_id"] == "n1"
    assert isinstance(call["response_schema"], dict)
    assert call["system_prompt"].strip()
    assert call["message"].strip()


async def test_test_generator_uses_delegator(quiet_context):
    import agents.test_generator as mod

    fake = FakeDelegator(
        {
            "summary": "tests planned",
            "tests": [
                {
                    "file_path": "backend/tests/x.spec.ts",
                    "type": "unit",
                    "name": "creates x",
                    "scenario_id": "s1",
                }
            ],
            "files_written": ["backend/tests/x.spec.ts"],
        }
    )
    backend = DelegatingAgentBackend(fake, name="test-delegator")

    generator = mod.TestGenerator(
        workspace_root=str(quiet_context),
        agent_backend=backend,
    )
    tests, output_text = await generator.run("n2", {"children_ids": []})

    assert isinstance(tests, list) and len(tests) == 1
    assert tests[0]["file_path"] == "backend/tests/x.spec.ts"
    assert "tests planned" in output_text
    assert fake.calls[0]["stage"] == "TestGenerator"


async def test_tdd_delegated_passes_when_system_tests_pass(quiet_context):
    import agents.test_driven_developer as mod

    fake = FakeDelegator({"summary": "implemented the feature"})
    backend = DelegatingAgentBackend(fake, name="test-delegator")
    executor_calls = []

    async def run_tests_executor(test_type, test_files):
        executor_calls.append((test_type, test_files))
        return "Exit Code: 0\nAll tests passed."

    developer = mod.TestDrivenDeveloper(
        workspace_root=str(quiet_context),
        agent_backend=backend,
    )
    result = await developer.run(
        node_id="n3",
        test_files=["backend/tests/x.spec.ts"],
        test_type="unit",
        run_tests_executor=run_tests_executor,
    )

    assert "IMPLEMENTED" in result.upper()
    assert executor_calls == [("unit", ["backend/tests/x.spec.ts"])]
    assert developer.get_last_run_tests_result() == "Exit Code: 0\nAll tests passed."
    assert fake.calls[0]["phase"] == "IMPLEMENT"


async def test_tdd_delegated_fails_when_system_tests_fail(quiet_context):
    import agents.test_driven_developer as mod

    fake = FakeDelegator({"summary": "claims done"})
    backend = DelegatingAgentBackend(fake, name="test-delegator")

    async def run_tests_executor(test_type, test_files):
        return "Exit Code: 1\nSTDERR:\nexpected 200, got 500"

    developer = mod.TestDrivenDeveloper(
        workspace_root=str(quiet_context),
        agent_backend=backend,
    )
    result = await developer.run(
        node_id="n4",
        test_files=["backend/tests/x.spec.ts"],
        test_type="unit",
        run_tests_executor=run_tests_executor,
    )

    assert "IMPLEMENTED" not in result.upper()
    assert developer.get_last_verifier_report()


async def test_end_to_end_interface_designer_through_real_delegator(
    monkeypatch, quiet_context, chat_server
):
    import agents.interface_designer as mod
    from integrations.stage_delegation import StageDelegator

    chat_server.post_responses["/api/messages"] = {"ok": True, "id": "msg_e2e_1"}
    chat_server.get_responses["/api/inbox/arc-compiler"] = {
        "dm": [
            {
                "id": "msg_reply_e2e",
                "from": "claude-implementer",
                "type": "reply",
                "reply_to": "msg_e2e_1",
                "summary": "done",
                "schema": {
                    "kind": "task_result",
                    "version": 1,
                    "payload": {
                        "ok": True,
                        "output": {
                            "summary": "e2e designed",
                            "interfaces": [{"name": "GET /api/tickets"}],
                            "files_written": [],
                        },
                    },
                },
            }
        ],
        "group": [],
    }
    delegator = StageDelegator(
        chat_server.base_url,
        implementer="claude-implementer",
        poll_interval=0.01,
        timeout=5.0,
    )
    backend = DelegatingAgentBackend(delegator, name="agent-chat")
    try:
        designer = mod.InterfaceDesigner(
            workspace_root=str(quiet_context),
            agent_backend=backend,
        )
        bundle = await designer.run(node_id="n-e2e", requirement_data={"children_ids": []})
    finally:
        await backend.aclose()

    assert bundle["summary"] == "e2e designed"
    assert bundle["interfaces"] == [{"name": "GET /api/tickets"}]
    sent = [r for r in chat_server.requests if r["path"].startswith("/api/messages")][0]["body"]
    assert sent["schema"]["payload"]["stage"] == "InterfaceDesigner"
    assert sent["schema"]["payload"]["workspace"] == str(quiet_context)
