from __future__ import annotations
import asyncio
from hermes_cli.config import load_config
from plugins.temporal.tconfig import resolve_temporal_config
from plugins.temporal.client import connect


async def run_worker(s) -> None:
    from tools.registry import discover_builtin_tools
    discover_builtin_tools()
    from temporalio.worker import Worker  # type: ignore
    from plugins.temporal.workflows import _make_workflow, _make_background_workflow, _make_human_input_workflow, _make_cron_fire_workflow, _make_kanban_task_workflow
    from plugins.temporal.activities import _make_activities
    client = await connect(s)
    worker = Worker(
        client,
        task_queue=s.task_queue,
        workflows=[_make_workflow(), _make_background_workflow(), _make_human_input_workflow(), _make_cron_fire_workflow(), _make_kanban_task_workflow()],
        activities=_make_activities(),
    )
    await worker.run()


def setup_worker_parser(subparsers) -> None:
    """Attach `hermes temporal worker` (called by register_cli_command setup_fn)."""
    subparsers.add_parser("worker", help="Run the Temporal worker for the hermes task queue")


def cmd_temporal_worker(args) -> int:
    s = resolve_temporal_config(load_config())
    if not s.enabled:
        print("temporal.enabled is false in config.yaml — nothing to run.")
        return 1
    asyncio.run(run_worker(s))
    return 0


def setup_respond_parser(subparsers) -> None:
    """Attach `hermes temporal respond` (called by register_cli_command setup_fn)."""
    p = subparsers.add_parser("respond", help="Answer a waiting durable_ask")
    p.add_argument("run_id")
    p.add_argument("answer")


def cmd_temporal(args) -> int:
    """Dispatch the `hermes temporal <subcommand>`."""
    if getattr(args, "temporal_command", None) == "respond":
        from plugins.temporal.tools import signal_human_input
        res = signal_human_input(args.run_id, args.answer, "", trusted=True)
        print(res.get("error") or f"Responded to {args.run_id}.")
        return 0 if res.get("status") == "ok" else 1
    return cmd_temporal_worker(args)  # default: worker
