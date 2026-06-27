"""Tests for the Turso/database-backend doctor check."""

import textwrap

from hermes_cli.doctor import _check_database_backend, _database_backend_status


def test_status_reports_sqlite_default(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text("model:\n  provider: openai\n", encoding="utf-8")
    ok, detail = _database_backend_status()
    assert ok
    assert "sqlite" in detail.lower()


def test_status_flags_turso_missing_token(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("TURSO_AUTH_TOKEN", raising=False)
    (tmp_path / "config.yaml").write_text(textwrap.dedent("""
        database:
          backend: turso
          turso:
            sync_url: "libsql://x.turso.io"
    """), encoding="utf-8")
    ok, detail = _database_backend_status()
    assert not ok
    assert "TURSO_AUTH_TOKEN" in detail


def test_check_reports_collision_free_when_turso_active(
    tmp_path, monkeypatch, capsys
):
    """When the Turso backend is active, the doctor must report that sync is
    collision-free (device-partitioned Snowflake ids) rather than the old
    last-push-wins warning."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("TURSO_AUTH_TOKEN", "tok-123")
    (tmp_path / "config.yaml").write_text(textwrap.dedent("""
        database:
          backend: turso
          turso:
            sync_url: "libsql://x.turso.io"
    """), encoding="utf-8")
    _check_database_backend([])
    out = capsys.readouterr().out.lower()
    assert "collision-free" in out or "device-partitioned" in out
    assert "drop" not in out


def test_check_does_not_warn_on_sqlite_default(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text("model:\n  provider: openai\n", encoding="utf-8")
    _check_database_backend([])
    out = capsys.readouterr().out.lower()
    assert "last-push-wins" not in out
