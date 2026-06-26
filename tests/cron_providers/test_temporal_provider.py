"""Tests for TemporalCronScheduler provider (Task 4 — Phase 4a).

Seam used: ``_reconcile`` accepts ``_list_fn``, ``_upsert_fn``, ``_delete_fn``
keyword-only callables so the async client_ops can be replaced synchronously in
unit tests without spawning an asyncio event loop or a real Temporal server.

Required assertions (brief §Task-4):
  1. enabled job  → upsert called
  2. orphan schedule id → delete called
  3. is_available() False when temporal.enabled is false
  4. load_cron_scheduler("temporal") returns the provider
  5. resolve_cron_scheduler() falls back to "builtin" when temporal disabled
"""
import threading

import pytest

from plugins.cron_providers.temporal import TemporalCronScheduler
from plugins.cron_providers import load_cron_scheduler
from cron.scheduler_provider import resolve_cron_scheduler


# ---------------------------------------------------------------------------
# 1 + 4  name & registry
# ---------------------------------------------------------------------------


def test_name_and_loads_via_registry():
    p = TemporalCronScheduler()
    assert p.name == "temporal"

    loaded = load_cron_scheduler("temporal")
    assert loaded is not None
    assert loaded.name == "temporal"


# ---------------------------------------------------------------------------
# 3  is_available gating
# ---------------------------------------------------------------------------


def test_is_available_requires_temporal_enabled(monkeypatch):
    p = TemporalCronScheduler()
    monkeypatch.setattr(p, "_temporal_enabled", lambda: False)
    assert p.is_available() is False

    monkeypatch.setattr(p, "_temporal_enabled", lambda: True)
    assert p.is_available() is True


# ---------------------------------------------------------------------------
# 5  resolver falls back to builtin when temporal disabled
# ---------------------------------------------------------------------------


def test_resolver_falls_back_when_unavailable(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"cron": {"provider": "temporal"}, "temporal": {"enabled": False}},
    )
    sched = resolve_cron_scheduler()
    assert sched.name == "builtin"  # fell back because is_available() returned False


# ---------------------------------------------------------------------------
# 1 + 2  reconcile: enabled job → upsert; orphan id → delete
# ---------------------------------------------------------------------------


def test_reconcile_calls_client_ops(monkeypatch):
    """_reconcile with injectable callables; no async event loop required."""
    p = TemporalCronScheduler()
    calls: dict = {"upsert": [], "delete": []}

    monkeypatch.setattr(
        "cron.jobs.list_jobs",
        lambda include_disabled=True: [
            {"id": "a", "enabled": True, "schedule": {"kind": "interval", "minutes": 5}}
        ],
    )

    p._reconcile(
        _list_fn=lambda: {"hermes-cron-gone"},
        _upsert_fn=lambda job: calls["upsert"].append(job["id"]),
        _delete_fn=lambda sid: calls["delete"].append(sid),
    )

    # Assertion 1: enabled job 'a' was upserted
    assert calls["upsert"] == ["a"]
    # Assertion 2: orphan 'hermes-cron-gone' was deleted
    assert "hermes-cron-gone" in calls["delete"]
