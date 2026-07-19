from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import time
from dataclasses import dataclass

from app_type_handler import list_app_types, normalize_app_type
from core.utils import cli_log, init_debug_logger, print_cli_banner, print_cli_startup, set_web_port, stop_cli_spinner
from core.workflow import ARCWorkflowManager
from integrations.agent_chat import build_reporter
from integrations.stage_delegation import build_stage_delegator, set_stage_delegator


@dataclass(slots=True)
class CompilationConfig:
    output_dir: str
    requirement_dir: str
    requirement_path: str
    user_requested_clear_all: bool = False
    app_type: str = "web"
    web_port: int = 3301
    resume_from_queue: bool = False


def _get_repo_root() -> str:
    return os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def _build_default_output_dir() -> str:
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    return os.path.join(_get_repo_root(), "workspace", f"run-{timestamp}")


def _resolve_requirement_dir(path: str) -> str:
    normalized = os.path.abspath(path)
    if not os.path.isdir(normalized):
        raise FileNotFoundError(f"Requirement directory does not exist: {normalized}")
    requirement_file = os.path.join(normalized, "requirements.yaml")
    if not os.path.isfile(requirement_file):
        raise FileNotFoundError(f"Requirement directory must contain requirements.yaml: {normalized}")
    return normalized


def _reset_directory(path: str) -> None:
    if os.path.isdir(path):
        shutil.rmtree(path, ignore_errors=True)
    os.makedirs(path, exist_ok=True)


def _copy_requirement_dir_contents(requirement_dir: str, output_dir: str) -> None:
    target_requirements_dir = os.path.join(output_dir, "requirements")
    os.makedirs(target_requirements_dir, exist_ok=True)
    for entry in os.listdir(requirement_dir):
        src = os.path.join(requirement_dir, entry)
        dst = os.path.join(target_requirements_dir, entry)
        if os.path.isdir(src):
            shutil.copytree(src, dst, dirs_exist_ok=True)
        else:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run ARC agent workflow from the command line.")
    parser.add_argument(
        "requirement_path",
        nargs="?",
        help="Requirement directory containing requirements.yaml and optional reference/ assets. Its contents will be copied into output-dir/requirements/ before compilation. Not needed with --serve.",
    )
    parser.add_argument(
        "--serve",
        action="store_true",
        help="Run as a resident agent-chat worker: heartbeat, poll the inbox for task_request messages, compile each request, and reply with task_result. Requires --agent-chat-url / AGENT_CHAT_URL.",
    )
    parser.add_argument(
        "--output-dir",
        help="Output workspace directory. Defaults to <repo_root>/workspace/run-<timestamp>.",
    )
    parser.add_argument(
        "--clear-all",
        action="store_true",
        help="Reset the output directory before copying the requirement directory and recompiling.",
    )
    parser.add_argument(
        "--app-type",
        choices=list_app_types(),
        default="web",
        help="Application type for runtime stack context.",
    )
    parser.add_argument(
        "--web-port",
        type=int,
        default=3000,
        help="Single backend port for web apps. Ignored by non-web app types.",
    )
    parser.add_argument(
        "--agent-chat-url",
        help="agent-chat backend base URL (e.g. http://127.0.0.1:8090) to report compilation progress to. Falls back to AGENT_CHAT_URL.",
    )
    parser.add_argument(
        "--agent-chat-group",
        help="agent-chat group to post progress into. Falls back to AGENT_CHAT_GROUP.",
    )
    parser.add_argument(
        "--agent-chat-to",
        help="agent-chat agent/human to DM progress to. Falls back to AGENT_CHAT_TO.",
    )
    parser.add_argument(
        "--delegate-to",
        help="Delegate stage execution (design/tests/implementation) to this agent-chat agent (e.g. a Claude Code or Codex agent) instead of calling an OpenAI-compatible API. Requires --agent-chat-url / AGENT_CHAT_URL. Falls back to ARC_DELEGATE_TO.",
    )
    return parser.parse_args()


def prepare_config(args: argparse.Namespace) -> CompilationConfig:
    return prepare_compilation(
        requirement_path=args.requirement_path,
        output_dir=args.output_dir,
        clear_all=args.clear_all,
        app_type=args.app_type,
        web_port=args.web_port,
    )


def prepare_compilation(
    requirement_path: str,
    output_dir: str | None = None,
    clear_all: bool = False,
    app_type: str = "web",
    web_port: int = 3000,
) -> CompilationConfig:
    normalized_output_dir = os.path.abspath(output_dir) if output_dir else _build_default_output_dir()
    normalized_requirement_dir = _resolve_requirement_dir(requirement_path)
    normalized_app_type = normalize_app_type(app_type)

    web_port = int(web_port)
    if normalized_app_type == "web" and (web_port < 1 or web_port > 65535):
        raise ValueError(f"Web port must be between 1 and 65535, got: {web_port}")

    if normalized_app_type == "web":
        set_web_port(web_port)
    queue_path = os.path.join(normalized_output_dir, ".arc", "processing_queue.json")
    resume_from_queue = (not clear_all) and os.path.exists(queue_path)

    if not resume_from_queue:
        _reset_directory(normalized_output_dir)
        _copy_requirement_dir_contents(normalized_requirement_dir, normalized_output_dir)

    normalized_requirement_path = os.path.join(normalized_output_dir, "requirements", "requirements.yaml")
    if not os.path.isfile(normalized_requirement_path):
        raise FileNotFoundError(
            f"Copied requirement workspace is missing requirements.yaml: {normalized_requirement_path}"
        )

    return CompilationConfig(
        output_dir=normalized_output_dir,
        requirement_dir=normalized_requirement_dir,
        requirement_path=normalized_requirement_path,
        user_requested_clear_all=clear_all,
        app_type=normalized_app_type,
        web_port=web_port,
        resume_from_queue=resume_from_queue,
    )


async def run_serve(args: argparse.Namespace) -> None:
    from integrations.agent_chat_worker import AgentChatWorker

    url = args.agent_chat_url or os.environ.get("AGENT_CHAT_URL")
    if not url:
        raise SystemExit("--serve requires --agent-chat-url or AGENT_CHAT_URL")
    try:
        reporter = build_reporter(
            url=args.agent_chat_url,
            group=args.agent_chat_group,
            to=args.agent_chat_to,
            run_label="serve",
        )
    except ValueError:
        reporter = None  # no progress target configured; task replies still work

    async def run_task(payload: dict) -> dict:
        config = prepare_compilation(
            requirement_path=payload["requirement_dir"],
            output_dir=payload.get("output_dir"),
            clear_all=bool(payload.get("clear_all", False)),
            app_type=payload.get("app_type", args.app_type),
            web_port=int(payload.get("web_port", args.web_port)),
        )
        log_path = init_debug_logger(config.output_dir, reset_existing=not config.resume_from_queue)
        log_cb = cli_log if reporter is None else reporter.make_log_cb(cli_log)
        workflow_manager = ARCWorkflowManager(
            workspace_path=config.output_dir,
            requirement_path=config.requirement_path,
            app_type=config.app_type,
            web_port=config.web_port,
            log_cb=log_cb,
        )
        raw = await workflow_manager.start_compilation(
            clear_all=False,
            resume_from_queue=config.resume_from_queue,
        ) or {}
        return {
            "ok": bool(raw.get("ok")),
            "failed_nodes": raw.get("failed_nodes") or [],
            "output_dir": config.output_dir,
            "log_path": log_path,
        }

    worker = AgentChatWorker(
        url,
        agent_name=os.environ.get("AGENT_CHAT_AGENT_NAME", "arc-compiler"),
        task_runner=run_task,
        api_token=os.environ.get("AGENT_CHAT_TOKEN"),
        agent_token=os.environ.get("AGENT_CHAT_AGENT_TOKEN"),
    )
    # Delegation is safe alongside the worker: tasks run sequentially inside
    # run_forever, so the delegator's preview polls never race a cursor-advancing
    # full inbox read.
    delegator = build_stage_delegator(url=args.agent_chat_url, implementer=args.delegate_to)
    if delegator is not None:
        set_stage_delegator(delegator)
    print(f"ARC agent-chat worker online as '{worker.agent_name}' -> {url} (Ctrl-C to stop)", flush=True)
    try:
        await worker.run_forever()
    finally:
        if delegator is not None:
            set_stage_delegator(None)
            await delegator.aclose()
        await worker.aclose()
        if reporter is not None:
            await reporter.aclose()


async def run() -> None:
    args = parse_args()
    if args.serve:
        await run_serve(args)
        return
    if not args.requirement_path:
        raise SystemExit("requirement_path is required unless --serve is used")
    config = prepare_config(args)
    reporter = build_reporter(
        url=args.agent_chat_url,
        group=args.agent_chat_group,
        to=args.agent_chat_to,
        run_label=os.path.basename(config.output_dir),
    )
    print_cli_banner()
    log_path = init_debug_logger(config.output_dir, reset_existing=not config.resume_from_queue)
    print_cli_startup(
        project_path=config.output_dir,
        requirement_path=config.requirement_path,
        app_type=config.app_type,
        clear_all=config.user_requested_clear_all,
        log_path=log_path,
        web_port=config.web_port,
        resume_from_queue=config.resume_from_queue,
    )
    log_cb = cli_log
    if reporter is not None:
        await reporter.register()
        log_cb = reporter.make_log_cb(cli_log)
    delegator = build_stage_delegator(url=args.agent_chat_url, implementer=args.delegate_to)
    if delegator is not None:
        set_stage_delegator(delegator)
    try:
        workflow_manager = ARCWorkflowManager(
            workspace_path=config.output_dir,
            requirement_path=config.requirement_path,
            app_type=config.app_type,
            web_port=config.web_port,
            log_cb=log_cb,
        )
        await workflow_manager.start_compilation(
            clear_all=False,
            resume_from_queue=config.resume_from_queue,
        )
    finally:
        stop_cli_spinner()
        if delegator is not None:
            set_stage_delegator(None)
            await delegator.aclose()
        if reporter is not None:
            await reporter.aclose()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
