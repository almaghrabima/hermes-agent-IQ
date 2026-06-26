"""Engine-agnostic database connection factory.

Default behaviour returns a stdlib ``sqlite3`` connection — byte-identical to
the previous direct ``sqlite3.connect()`` calls. When ``database.backend`` is
``turso`` in ``config.yaml``, returns a libSQL embedded replica that syncs to a
Turso cloud database in the background.

Two meanings of "sync" are deliberately separate: ``conn.sync()`` is *replica
synchronization*; the connection API itself stays synchronous (Hermes's core
loop is synchronous by design). The internal engine is swappable (libsql today,
pyturso later) without changing this module's public surface.
"""
from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hermes_constants import get_hermes_home

_AUTH_TOKEN_ENV = "TURSO_AUTH_TOKEN"


class BackendConfigError(RuntimeError):
    """``database.backend: turso`` is selected but required fields are missing.

    Raised at resolve time so startup fails loudly with a clear message rather
    than silently falling back to a local-only DB (which would split-brain the
    user's data across devices).
    """


@dataclass
class SyncConfig:
    sync_url: str
    auth_token: str
    sync_interval: int = 60
    local_path: Path | None = None


def resolve_sync_config(label: str) -> SyncConfig | None:
    """Return a ``SyncConfig`` when ``database.backend`` is ``turso``, else None.

    ``label`` names the logical database (e.g. ``"state.db"``) and is used to
    derive the default local replica path under ``<hermes_home>/replicas/``.
    """
    from hermes_cli.config import load_config

    cfg = load_config() or {}
    db_cfg = cfg.get("database")
    if not isinstance(db_cfg, dict):
        return None
    backend = str(db_cfg.get("backend") or "sqlite").strip().lower()
    if backend != "turso":
        return None

    turso = db_cfg.get("turso")
    turso = turso if isinstance(turso, dict) else {}

    sync_url = str(turso.get("sync_url") or "").strip()
    if not sync_url:
        raise BackendConfigError(
            "database.backend is 'turso' but database.turso.sync_url is not set "
            "in config.yaml."
        )

    auth_token = (os.environ.get(_AUTH_TOKEN_ENV) or "").strip()
    if not auth_token:
        raise BackendConfigError(
            f"database.backend is 'turso' but {_AUTH_TOKEN_ENV} is not set in "
            f"{get_hermes_home() / '.env'}."
        )

    try:
        sync_interval = int(turso.get("sync_interval", 60))
    except (TypeError, ValueError):
        sync_interval = 60

    raw_path = turso.get("local_path")
    if raw_path:
        local_path = Path(str(raw_path)).expanduser()
    else:
        local_path = get_hermes_home() / "replicas" / label

    return SyncConfig(
        sync_url=sync_url,
        auth_token=auth_token,
        sync_interval=sync_interval,
        local_path=local_path,
    )
