"""Temporal-backed kanban spawn plugin.

Provides ``temporal_kanban_spawn(task, workspace, *, board=None)`` as a drop-in
replacement for ``kanban_db._default_spawn``.  All Temporal imports are deferred
inside functions so importing this module at top-level does NOT pull in temporalio.
"""
from __future__ import annotations

import asyncio
import logging

log = logging.getLogger(__name__)


def _start_kanban_workflow(task, workspace: str, board) -> None:
    """Connect to Temporal and start a KanbanTaskWorkflow for *task*.

    Factored out so tests can monkeypatch this single function to simulate
    a connect failure without needing a live Temporal server.
    """
    from hermes_cli.kanban_db import build_spawn_args
    from hermes_cli.config import load_config
    from plugins.temporal.tconfig import resolve_temporal_config
    from plugins.temporal.client import connect

    spawn_args = build_spawn_args(task, workspace, board=board)
    cfg = load_config()
    failure_limit = (cfg.get("kanban") or {}).get("failure_limit", 2)
    s = resolve_temporal_config(cfg)

    async def _run():
        client = await connect(s)
        await client.start_workflow(
            "KanbanTaskWorkflow",
            {
                "task_id": task.id,
                "spawn_args": spawn_args,
                "board": board,
                "failure_limit": failure_limit,
            },
            id=f"hermes-kanban-{task.id}-{task.current_run_id}",
            task_queue=s.task_queue,
        )

    asyncio.run(_run())


def temporal_kanban_spawn(task, workspace: str, *, board=None):
    """Start a KanbanTaskWorkflow for *task* and return None (PID unknown).

    On ANY exception falls back to the builtin subprocess spawn so the
    dispatcher never gets stuck on a Temporal outage.
    """
    try:
        _start_kanban_workflow(task, workspace, board)
        return None
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "temporal_kanban_spawn: failed to start KanbanTaskWorkflow for task %s "
            "(%s); falling back to builtin subprocess spawn.",
            getattr(task, "id", "?"),
            exc,
        )
        from hermes_cli.kanban_db import _default_spawn
        return _default_spawn(task, workspace, board=board)
