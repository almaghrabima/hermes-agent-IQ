from __future__ import annotations

import shlex as _shlex

from hermes_cli.config import load_config
from plugins.temporal.tconfig import resolve_temporal_config
from plugins.temporal import tools as _tools
from plugins.temporal.worker import setup_worker_parser, cmd_temporal_worker, setup_respond_parser, cmd_temporal


def _respond_command(raw_args: str) -> str:
    """/respond <run_id> "<answer>" — signal a waiting durable_ask."""
    try:
        parts = _shlex.split(raw_args or "")
    except ValueError:
        parts = (raw_args or "").split()
    if len(parts) < 2:
        return 'usage: /respond <run_id> "<answer>"'
    run_id, answer = parts[0], " ".join(parts[1:])
    from tools.approval import get_current_session_key
    from plugins.temporal import tools as _t
    res = _t.signal_human_input(run_id, answer, get_current_session_key(default="default"))
    if res.get("status") == "ok":
        return f"Responded to {run_id}."
    return f"respond error: {res.get('error')}"


def temporal_available() -> bool:
    """check_fn: tools appear only when temporal is enabled and a target is set."""
    try:
        s = resolve_temporal_config(load_config())
        return bool(s.enabled and s.target)
    except Exception:
        return False


def register(ctx) -> None:
    ctx.register_tool(
        name="durable_run", toolset="temporal",
        schema=_tools.DURABLE_RUN_SCHEMA, handler=_tools.handle_durable_run,
        check_fn=temporal_available, description="Run a durable, retrying multi-step job.",
        emoji="⏱️",
    )
    ctx.register_tool(
        name="durable_status", toolset="temporal",
        schema=_tools.DURABLE_STATUS_SCHEMA, handler=_tools.handle_durable_status,
        check_fn=temporal_available, description="Check a durable_run by run_id.",
        emoji="⏱️",
    )
    ctx.register_tool(
        name="durable_ask", toolset="temporal",
        schema=_tools.DURABLE_ASK_SCHEMA, handler=_tools.handle_durable_ask,
        check_fn=temporal_available, description="Pause durably for human input.",
        emoji="⏸️",
    )

    ctx.register_command(name="respond", handler=_respond_command,
                         description="Answer a waiting durable_ask",
                         args_hint="<run_id> <answer>")

    def _setup(subparser):
        sub = subparser.add_subparsers(dest="temporal_command")
        setup_worker_parser(sub)
        setup_respond_parser(sub)

    ctx.register_cli_command(
        name="temporal", help="Temporal worker / durable orchestration",
        setup_fn=_setup, handler_fn=cmd_temporal,
        description="Run `hermes temporal worker` to execute durable workflows.",
    )
