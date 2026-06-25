from __future__ import annotations
import asyncio
from hermes_cli.config import load_config
from plugins.temporal.tconfig import resolve_temporal_config
from plugins.temporal.client import connect


async def run_worker(s) -> None:
    from temporalio.worker import Worker  # type: ignore
    from plugins.temporal.workflows import _make_workflow
    from plugins.temporal.activities import _make_activity
    client = await connect(s)
    worker = Worker(
        client,
        task_queue=s.task_queue,
        workflows=[_make_workflow()],
        activities=[_make_activity()],
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
