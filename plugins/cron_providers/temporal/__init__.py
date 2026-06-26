"""Temporal — Temporal.io-mediated external cron provider.

Temporal schedules are long-lived entities managed by the Temporal server.
Once upserted here, they fire independently of this process via the Temporal
worker. This provider's job is to *reconcile* Hermes job definitions into
Temporal schedules (upsert desired, delete orphaned), not to fire jobs itself.

Design constraints:
  - ``start()`` reconciles once then loops on ``stop_event.wait`` for periodic
    self-healing (Temporal server may be transiently unavailable at startup).
    Actual job firing is driven by the Temporal worker, not this loop.
  - ``is_available()`` is config-only — no network calls.  Falls back to the
    built-in in-process ticker when unavailable (``resolve_cron_scheduler``).
  - temporalio imports are lazy (inside client_ops functions); this module
    imports nothing from temporalio at module scope.

Inert unless ``cron.provider: temporal`` and ``temporal.enabled: true``.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any, Callable, Optional

from cron.scheduler_provider import CronScheduler

logger = logging.getLogger("cron_providers.temporal")


class TemporalCronScheduler(CronScheduler):
    """Temporal.io external cron scheduler provider."""

    # -- identity / availability -----------------------------------------

    @property
    def name(self) -> str:
        return "temporal"

    def _temporal_enabled(self) -> bool:
        """Return True if ``temporal.enabled`` is truthy in config (no network)."""
        try:
            from hermes_cli.config import cfg_get, load_config
            return bool(cfg_get(load_config(), "temporal", "enabled", default=False))
        except Exception:
            return False

    def is_available(self) -> bool:
        """Config-only check — NO network calls."""
        return self._temporal_enabled()

    # -- async helper -----------------------------------------------------

    def _run_async(self, coro: Any) -> Any:
        """Run an async coroutine synchronously.

        Monkeypatching this method in tests lets callers intercept
        async client_ops calls without a real event loop.
        """
        return asyncio.run(coro)

    # -- reconcile (unit-testable seam) -----------------------------------

    def _reconcile(
        self,
        *,
        _list_fn: Optional[Callable[[], Any]] = None,
        _upsert_fn: Optional[Callable[[dict], Any]] = None,
        _delete_fn: Optional[Callable[[str], Any]] = None,
    ) -> None:
        """Converge Temporal schedules toward jobs.json (desired state).

        Injectable callables make this unit-testable without a Temporal server:

          p._reconcile(
              _list_fn=lambda: {"hermes-cron-gone"},
              _upsert_fn=lambda job: ...,
              _delete_fn=lambda sid: ...,
          )

        In production the callables default to async client_ops invocations
        via ``_run_async``.
        """
        from cron.jobs import list_jobs
        from plugins.cron_providers.temporal.schedules import plan_reconcile

        jobs = list_jobs(include_disabled=True)

        # Resolve callables — lazy-import client_ops only when needed so
        # temporalio is never imported at module scope.
        if _list_fn is None:
            from plugins.cron_providers.temporal import client_ops as _co
            list_fn: Callable[[], Any] = lambda: self._run_async(_co.list_hermes_schedule_ids())
        else:
            list_fn = _list_fn

        if _upsert_fn is None:
            from plugins.cron_providers.temporal import client_ops as _co  # noqa: F811
            upsert_fn: Callable[[dict], Any] = lambda job: self._run_async(_co.upsert_schedule(job))
        else:
            upsert_fn = _upsert_fn

        if _delete_fn is None:
            from plugins.cron_providers.temporal import client_ops as _co  # noqa: F811
            delete_fn: Callable[[str], Any] = lambda sid: self._run_async(_co.delete_schedule(sid))
        else:
            delete_fn = _delete_fn

        try:
            existing = list_fn()
        except Exception as exc:
            logger.warning("temporal cron reconcile: list failed (%s); will retry", exc)
            return

        plan = plan_reconcile(jobs, existing)
        by_id = {j["id"]: j for j in jobs}

        for jid in plan["upsert"]:
            try:
                upsert_fn(by_id[jid])
            except Exception as exc:
                logger.warning("temporal cron: upsert %s failed: %s", jid, exc)

        for sid in plan["delete"]:
            try:
                delete_fn(sid)
            except Exception as exc:
                logger.warning("temporal cron: delete %s failed: %s", sid, exc)

    # -- lifecycle --------------------------------------------------------

    def start(
        self,
        stop_event: threading.Event,
        *,
        adapters: Any = None,
        loop: Any = None,
        interval: int = 60,
    ) -> None:
        """Reconcile Hermes jobs into Temporal schedules, then self-heal.

        The initial reconcile syncs all enabled jobs.  The loop then wakes
        every ``interval`` seconds (minimum 60) to catch transient failures and
        any changes that bypassed ``on_jobs_changed`` (e.g. external edits).
        Actual job *firing* is handled by the Temporal worker — this loop only
        keeps schedule definitions in sync.
        """
        logger.info(
            "Temporal cron scheduler started (schedules drive firing via the Temporal worker)"
        )
        try:
            self._reconcile()
        except Exception as exc:
            logger.warning("Temporal cron start() reconcile failed: %s", exc)

        while not stop_event.is_set():
            stop_event.wait(max(interval, 60))
            if stop_event.is_set():
                break
            try:
                self._reconcile()
            except Exception as exc:
                logger.warning("Temporal cron periodic reconcile failed: %s", exc)

    def stop(self) -> None:
        return None

    def on_jobs_changed(self) -> None:
        """Called after a job mutation — reconcile immediately."""
        try:
            self._reconcile()
        except Exception as exc:
            logger.warning("temporal cron on_jobs_changed: %s", exc)

    def reconcile(self) -> None:
        """Public reconcile hook (CronScheduler ABC default override)."""
        self._reconcile()


# ---------------------------------------------------------------------------
# Plugin entrypoint
# ---------------------------------------------------------------------------


def register(ctx) -> None:  # type: ignore[override]
    """Register this provider with the cron scheduler plugin loader."""
    ctx.register_cron_scheduler(TemporalCronScheduler())
