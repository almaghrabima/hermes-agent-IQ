"""Tests for the Turso/database-backend doctor check."""

import textwrap

import pytest

from hermes_cli.doctor import _database_backend_status


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
