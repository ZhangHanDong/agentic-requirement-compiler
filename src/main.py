from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import sys
import time
from dataclasses import dataclass
from typing import Any

from agents.backend import (
    BuiltinAgentBackend,
    DelegatingAgentBackend,
    LocalOctosBackend,
)
from app_type_handler import list_app_types, normalize_app_type
from core.utils import (
    cli_log,
    init_debug_logger,
    print_cli_banner,
    print_cli_startup,
    print_compilation_summary,
    set_web_port,
    stop_cli_spinner,
)
from core.workflow import ARCWorkflowManager
from integrations.agent_chat import AgentChatReporter, build_reporter
from integrations.octos_mcp import (
    LocalOctosMcpDelegator,
    OctosMcpDelegator,
    build_local_octos_delegator,
    build_octos_delegator,
)
from integrations.stage_delegation import build_stage_delegator


@dataclass(slots=True)
class CompilationConfig:
    output_dir: str
    requirement_dir: str
    requirement_path: str
    user_requested_clear_all: bool = False
    app_type: str = "web"
    web_port: int = 3301
    resume_from_queue: bool = False
    retry_failed: bool = False
    retry_node_ids: list[str] | None = None
    model_api_mode: str | None = None


def _get_repo_root() -> str:
    return os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def _build_default_output_dir() -> str:
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    return os.path.join(_get_repo_root(), "workspace", f"run-{timestamp}")


def _ensure_dotenv_loaded() -> None:
    """Load .env file if present, respecting ARC_ENV_FILE override."""
    from dotenv import load_dotenv

    custom_env = os.environ.get("ARC_ENV_FILE", "").strip()
    if custom_env and os.path.isfile(custom_env):
        load_dotenv(custom_env, override=False)
        return

    default_env = os.path.join(_get_repo_root(), ".env")
    if os.path.isfile(default_env):
        load_dotenv(default_env, override=False)


def _locate_requirement_file(input_path: str) -> tuple[str, str, str]:
    """Return the requirement directory, file path, and file name."""
    abs_input = os.path.abspath(input_path)

    if os.path.isfile(abs_input):
        if not abs_input.endswith((".yaml", ".yml")):
            raise ValueError(f"Input file must be .yaml or .yml: {abs_input}")
        return os.path.dirname(abs_input), abs_input, os.path.basename(abs_input)

    if os.path.isdir(abs_input):
        for candidate in ("requirements.yaml", "requirements.yml"):
            candidate_path = os.path.join(abs_input, candidate)
            if os.path.isfile(candidate_path):
                return abs_input, candidate_path, candidate
        raise FileNotFoundError(f"No requirements.yaml found in {abs_input}")

    raise FileNotFoundError(f"Input path not found: {abs_input}")


def _add_agent_backend_arguments(
    parser: argparse.ArgumentParser,
    *,
    progress: bool,
    local_octos: bool = False,
) -> None:
    parser.add_argument(
        "--agent-chat-url",
        help="agent-chat backend base URL. Falls back to AGENT_CHAT_URL.",
    )
    if progress:
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
        help="Delegate stage execution to this agent-chat agent. Falls back to ARC_DELEGATE_TO.",
    )
    parser.add_argument(
        "--octos-mcp",
        help="Delegate stage execution to an octos MCP server. Falls back to OCTOS_MCP_URL.",
    )
    if local_octos:
        parser.add_argument(
            "--agent-backend",
            choices=("builtin", "octos-local"),
            help=(
                "Agent execution backend. Falls back to ARC_AGENT_BACKEND. "
                "Legacy Octos/delegation flags remain supported."
            ),
        )
        parser.add_argument(
            "--octos-local",
            action="store_true",
            help="Run a local octos MCP subprocess over stdio. Falls back to ARC_OCTOS_LOCAL.",
        )
        parser.add_argument(
            "--octos-bin",
            help="Path to the local octos executable. Implies --octos-local; falls back to OCTOS_BIN.",
        )


def _resolve_progress_reporter(
    args: argparse.Namespace,
    *,
    run_label: str,
) -> AgentChatReporter | None:
    group = getattr(args, "agent_chat_group", None) or os.environ.get("AGENT_CHAT_GROUP")
    to = getattr(args, "agent_chat_to", None) or os.environ.get("AGENT_CHAT_TO")
    if not group and not to:
        return None
    return build_reporter(
        url=args.agent_chat_url,
        group=group,
        to=to,
        run_label=run_label,
    )


def _env_truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def resolve_stage_delegator(
    args: argparse.Namespace,
    *,
    workspace_root: str | None = None,
):
    """Pick the configured stage-delegation backend, if any."""
    selected_backend = (
        getattr(args, "agent_backend", None)
        or os.environ.get("ARC_AGENT_BACKEND", "")
    ).strip().lower()
    if selected_backend and selected_backend not in {"builtin", "octos-local"}:
        raise SystemExit(
            "ARC_AGENT_BACKEND must be one of: builtin, octos-local"
        )
    local_requested = bool(
        getattr(args, "octos_local", False)
        or getattr(args, "octos_bin", None)
        or _env_truthy("ARC_OCTOS_LOCAL")
    )
    octos_requested = bool(args.octos_mcp or os.environ.get("OCTOS_MCP_URL"))
    agent_requested = bool(
        (args.delegate_to or os.environ.get("ARC_DELEGATE_TO"))
        and (args.agent_chat_url or os.environ.get("AGENT_CHAT_URL"))
    )
    if selected_backend == "builtin":
        if local_requested or octos_requested or agent_requested:
            raise SystemExit(
                "--agent-backend builtin conflicts with Octos or agent-chat "
                "delegation configuration"
            )
        return None
    if selected_backend == "octos-local":
        local_requested = True
    if sum((local_requested, octos_requested, agent_requested)) > 1:
        raise SystemExit(
            "--octos-local, --octos-mcp, and --delegate-to are mutually exclusive; "
            "pick one backend"
        )
    if local_requested:
        if not workspace_root:
            raise SystemExit("--octos-local requires an ARC output workspace")
        try:
            return build_local_octos_delegator(
                getattr(args, "octos_bin", None),
                workspace_root=workspace_root,
            )
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
    if octos_requested:
        return build_octos_delegator(url=args.octos_mcp)
    if agent_requested:
        return build_stage_delegator(url=args.agent_chat_url, implementer=args.delegate_to)
    return None


def resolve_agent_backend(
    args: argparse.Namespace,
    *,
    workspace_root: str | None = None,
):
    """Resolve the unified backend consumed by all compiled agent tasks."""

    delegator = resolve_stage_delegator(args, workspace_root=workspace_root)
    if delegator is None:
        return BuiltinAgentBackend()
    if isinstance(delegator, LocalOctosMcpDelegator):
        return LocalOctosBackend(delegator)
    if isinstance(delegator, OctosMcpDelegator):
        return DelegatingAgentBackend(delegator, name="octos-http")
    return DelegatingAgentBackend(delegator, name="agent-chat")


def build_compile_parser(subparsers: Any) -> None:
    parser = subparsers.add_parser(
        "compile",
        help="Compile requirements into a working application",
        description="Run ARC compilation from requirement tree to interfaces, tests, and implementation.",
    )
    parser.add_argument(
        "requirement_path",
        help="Path to requirements directory or .yaml file",
    )
    parser.add_argument(
        "-o",
        "--output-dir",
        required=True,
        help="Output workspace directory",
    )
    parser.add_argument(
        "-t",
        "--type",
        dest="app_type",
        default="web",
        help=f"Application type (choices: {', '.join(list_app_types())})",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=3301,
        help="Web server port (only for app-type=web, default: 3301)",
    )
    parser.add_argument(
        "--clean",
        action="store_true",
        help="Remove existing output directory before compilation",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume from saved compilation queue",
    )
    parser.add_argument(
        "--retry-failed",
        action="store_true",
        help="Retry all failed nodes from previous run (requires --resume)",
    )
    parser.add_argument(
        "--retry",
        nargs="+",
        metavar="NODE_ID",
        help="Retry specific node IDs (requires --resume)",
    )
    _add_agent_backend_arguments(parser, progress=True, local_octos=True)
    parser.set_defaults(func=cmd_compile)


async def cmd_compile(args: argparse.Namespace) -> int:
    """Execute the compile subcommand."""
    _ensure_dotenv_loaded()

    if args.clean and args.resume:
        print("Error: --clean and --resume are mutually exclusive")
        return 2
    if (args.retry_failed or args.retry) and not args.resume:
        print("Error: --retry-failed and --retry require --resume")
        return 2
    if args.retry_failed and args.retry:
        print("Error: --retry-failed and --retry are mutually exclusive")
        return 2

    requirement_dir, requirement_path, _ = _locate_requirement_file(args.requirement_path)
    output_dir = os.path.abspath(args.output_dir)
    if args.clean and os.path.exists(output_dir):
        shutil.rmtree(output_dir)

    normalized_app_type = normalize_app_type(args.app_type)
    set_web_port(args.port)
    config = CompilationConfig(
        output_dir=output_dir,
        requirement_dir=requirement_dir,
        requirement_path=requirement_path,
        user_requested_clear_all=args.clean,
        app_type=normalized_app_type,
        web_port=args.port,
        resume_from_queue=args.resume,
        retry_failed=args.retry_failed,
        retry_node_ids=args.retry or None,
        model_api_mode=os.environ.get("ARC_OPENAI_API_MODE", "").strip() or None,
    )

    reporter = _resolve_progress_reporter(args, run_label=os.path.basename(config.output_dir))
    agent_backend = resolve_agent_backend(args, workspace_root=config.output_dir)
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
        retry_failed=config.retry_failed,
        retry_node_ids=config.retry_node_ids,
        model_api_mode=config.model_api_mode,
    )

    result: dict[str, Any] = {"ok": False, "failed_nodes": []}
    start_time = time.time()
    try:
        log_cb = cli_log
        if reporter is not None:
            await reporter.register()
            log_cb = reporter.make_log_cb(cli_log)
        workflow_manager = ARCWorkflowManager(
            workspace_path=config.output_dir,
            requirement_path=config.requirement_path,
            app_type=config.app_type,
            web_port=config.web_port,
            log_cb=log_cb,
            agent_backend=agent_backend,
        )
        result = await workflow_manager.start_compilation(
            clear_all=False,
            resume_from_queue=config.resume_from_queue,
            retry_failed=config.retry_failed,
            retry_node_ids=config.retry_node_ids,
        )
    finally:
        stop_cli_spinner()
        await agent_backend.aclose()
        if reporter is not None:
            await reporter.aclose()

    print_compilation_summary(result, config.output_dir, time.time() - start_time)
    return 0 if result.get("ok") else 1


def build_serve_parser(subparsers: Any) -> None:
    parser = subparsers.add_parser(
        "serve",
        help="Run ARC as a resident agent-chat worker",
    )
    parser.add_argument(
        "-t",
        "--type",
        dest="app_type",
        default="web",
        help=f"Default application type (choices: {', '.join(list_app_types())})",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=3301,
        help="Default web server port",
    )
    _add_agent_backend_arguments(parser, progress=True)
    parser.set_defaults(func=cmd_serve)


def _prepare_worker_config(payload: dict[str, Any], args: argparse.Namespace) -> CompilationConfig:
    requirement_dir, requirement_path, _ = _locate_requirement_file(payload["requirement_dir"])
    output_dir = os.path.abspath(payload.get("output_dir") or _build_default_output_dir())
    clear_all = bool(payload.get("clear_all", False))
    if clear_all and os.path.exists(output_dir):
        shutil.rmtree(output_dir)

    retry_node_ids = [
        str(node_id).strip()
        for node_id in (payload.get("retry_node_ids") or payload.get("retry") or [])
        if str(node_id).strip()
    ]
    retry_failed = bool(payload.get("retry_failed", False))
    queue_path = os.path.join(output_dir, ".arc", "processing_queue.json")
    resume = bool(payload["resume"]) if "resume" in payload else (not clear_all and os.path.exists(queue_path))
    if (retry_failed or retry_node_ids) and not resume:
        raise ValueError("Worker retry options require an existing queue or resume=true")

    model_api_mode = str(payload.get("model_api_mode") or "").strip() or None
    if model_api_mode:
        os.environ["ARC_OPENAI_API_MODE"] = model_api_mode

    app_type = normalize_app_type(payload.get("app_type", args.app_type))
    web_port = int(payload.get("web_port", payload.get("port", args.port)))
    set_web_port(web_port)
    return CompilationConfig(
        output_dir=output_dir,
        requirement_dir=requirement_dir,
        requirement_path=requirement_path,
        user_requested_clear_all=clear_all,
        app_type=app_type,
        web_port=web_port,
        resume_from_queue=resume,
        retry_failed=retry_failed,
        retry_node_ids=retry_node_ids or None,
        model_api_mode=model_api_mode or os.environ.get("ARC_OPENAI_API_MODE", "").strip() or None,
    )


async def cmd_serve(args: argparse.Namespace) -> int:
    """Run as a dispatchable agent-chat worker."""
    from integrations.agent_chat_worker import AgentChatWorker

    _ensure_dotenv_loaded()
    url = args.agent_chat_url or os.environ.get("AGENT_CHAT_URL")
    if not url:
        raise SystemExit("serve requires --agent-chat-url or AGENT_CHAT_URL")

    reporter = _resolve_progress_reporter(args, run_label="serve")
    agent_backend = resolve_agent_backend(args)

    async def run_task(payload: dict[str, Any]) -> dict[str, Any]:
        config = _prepare_worker_config(payload, args)
        log_path = init_debug_logger(config.output_dir, reset_existing=not config.resume_from_queue)
        log_cb = cli_log if reporter is None else reporter.make_log_cb(cli_log)
        workflow_manager = ARCWorkflowManager(
            workspace_path=config.output_dir,
            requirement_path=config.requirement_path,
            app_type=config.app_type,
            web_port=config.web_port,
            log_cb=log_cb,
            agent_backend=agent_backend,
        )
        raw = await workflow_manager.start_compilation(
            clear_all=False,
            resume_from_queue=config.resume_from_queue,
            retry_failed=config.retry_failed,
            retry_node_ids=config.retry_node_ids,
        )
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
    print(f"ARC agent-chat worker online as '{worker.agent_name}' -> {url} (Ctrl-C to stop)", flush=True)
    try:
        await worker.run_forever()
    finally:
        await agent_backend.aclose()
        await worker.aclose()
        if reporter is not None:
            await reporter.aclose()
    return 0


def build_doctor_parser(subparsers: Any) -> None:
    build_config_parser(subparsers)
    parser = subparsers.add_parser(
        "doctor",
        help="Check ARC configuration and environment",
        description="Validate configuration, check dependencies, and diagnose common issues.",
    )
    parser.set_defaults(func=cmd_doctor)


def cmd_doctor(args: argparse.Namespace) -> int:
    """Execute the doctor subcommand."""
    _ensure_dotenv_loaded()
    from config_validator import print_health_check

    return print_health_check()


def build_config_parser(subparsers: Any) -> None:
    parser = subparsers.add_parser(
        "config",
        help="Configure ARC interactively",
        description="Create or update .env file with core configuration.",
    )
    parser.set_defaults(func=cmd_config)


def cmd_config(args: argparse.Namespace) -> int:
    """Execute the config subcommand."""
    from config_validator import interactive_config_setup

    return interactive_config_setup()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="arc",
        description="ARC: Agentic Requirement Compiler",
    )
    parser.add_argument(
        "--version",
        action="version",
        version="ARC 1.1.0",
    )
    subparsers = parser.add_subparsers(
        dest="command",
        required=True,
        help="Available commands",
    )
    build_compile_parser(subparsers)
    build_serve_parser(subparsers)
    build_doctor_parser(subparsers)
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    if asyncio.iscoroutinefunction(args.func):
        exit_code = asyncio.run(args.func(args))
    else:
        exit_code = args.func(args)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
