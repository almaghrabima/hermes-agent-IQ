"""Resolve the kanban worker spawn backend (cron-style provider seam)."""
from __future__ import annotations
import logging
from typing import Callable, Optional

log = logging.getLogger(__name__)


def resolve_kanban_spawn(cfg: Optional[dict] = None) -> Callable:
    """Return the spawn callable for the configured ``kanban.spawn_provider``.

    Default/fallback is the builtin subprocess spawn. ``temporal`` requires
    ``temporal.enabled`` true; otherwise (or on import failure) we log and
    fall back to builtin so kanban is never left without a spawn.
    The returned callable always accepts ``(task, workspace, *, board=None)``.
    """
    from hermes_cli.kanban_db import _default_spawn

    if cfg is None:
        from hermes_cli.config import load_config
        cfg = load_config()
    provider = ((cfg.get("kanban") or {}).get("spawn_provider") or "builtin").strip().lower()
    if provider == "builtin":
        return _default_spawn
    if provider == "temporal":
        if not bool((cfg.get("temporal") or {}).get("enabled")):
            log.warning(
                "kanban.spawn_provider=temporal but temporal.enabled is false; "
                "falling back to builtin subprocess spawn.")
            return _default_spawn
        try:
            from plugins.kanban_spawn_temporal import temporal_kanban_spawn
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "kanban.spawn_provider=temporal but the plugin failed to load (%s); "
                "falling back to builtin subprocess spawn.", exc)
            return _default_spawn
        temporal_kanban_spawn._kanban_run_kind = "temporal"  # type: ignore[attr-defined]
        return temporal_kanban_spawn
    log.warning("unknown kanban.spawn_provider=%r; using builtin.", provider)
    return _default_spawn
