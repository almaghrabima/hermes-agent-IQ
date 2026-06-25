from __future__ import annotations

from hermes_cli.config import load_config
from plugins.temporal.tconfig import resolve_temporal_config
from plugins.temporal import tools as _tools
from plugins.temporal.worker import setup_worker_parser, cmd_temporal_worker


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

    def _setup(subparser):
        sub = subparser.add_subparsers(dest="temporal_command")
        setup_worker_parser(sub)

    ctx.register_cli_command(
        name="temporal", help="Temporal worker / durable orchestration",
        setup_fn=_setup, handler_fn=cmd_temporal_worker,
        description="Run `hermes temporal worker` to execute durable workflows.",
    )
