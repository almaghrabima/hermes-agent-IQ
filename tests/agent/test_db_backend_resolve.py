import textwrap
from pathlib import Path

import pytest


def _write_config(home: Path, body: str) -> None:
    (home / "config.yaml").write_text(textwrap.dedent(body), encoding="utf-8")


def test_no_database_block_returns_none(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _write_config(tmp_path, "model:\n  provider: openai\n")
    from agent.db_backend import resolve_sync_config
    assert resolve_sync_config("state.db") is None


def test_backend_sqlite_returns_none(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _write_config(tmp_path, "database:\n  backend: sqlite\n")
    from agent.db_backend import resolve_sync_config
    assert resolve_sync_config("state.db") is None


def test_backend_turso_resolves_full_config(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("TURSO_AUTH_TOKEN", "tok-123")
    _write_config(tmp_path, """
        database:
          backend: turso
          turso:
            sync_url: "libsql://x.turso.io"
            sync_interval: 30
    """)
    from agent.db_backend import resolve_sync_config
    cfg = resolve_sync_config("state.db")
    assert cfg is not None
    assert cfg.sync_url == "libsql://x.turso.io"
    assert cfg.auth_token == "tok-123"
    assert cfg.sync_interval == 30
    # default local_path lands under HERMES_HOME/replicas/<label>
    assert cfg.local_path == tmp_path / "replicas" / "state.db"


def test_backend_turso_missing_token_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("TURSO_AUTH_TOKEN", raising=False)
    _write_config(tmp_path, """
        database:
          backend: turso
          turso:
            sync_url: "libsql://x.turso.io"
    """)
    from agent.db_backend import resolve_sync_config, BackendConfigError
    with pytest.raises(BackendConfigError, match="TURSO_AUTH_TOKEN"):
        resolve_sync_config("state.db")


def test_backend_turso_missing_sync_url_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("TURSO_AUTH_TOKEN", "tok-123")
    _write_config(tmp_path, "database:\n  backend: turso\n  turso: {}\n")
    from agent.db_backend import resolve_sync_config, BackendConfigError
    with pytest.raises(BackendConfigError, match="sync_url"):
        resolve_sync_config("state.db")
