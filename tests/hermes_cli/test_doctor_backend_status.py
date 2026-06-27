"""Tests that the database-backend doctor check reports collision-free sync status."""


def test_turso_status_reports_collision_free(capsys, monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    import importlib, hermes_cli.doctor as doc
    importlib.reload(doc)
    # Force the turso branch by stubbing the status detail
    monkeypatch.setattr(doc, "_database_backend_status",
                        lambda: (True, "Database backend: turso (sync_url=libsql://x, interval=60s)."))
    issues = []
    doc._check_database_backend(issues)
    out = capsys.readouterr().out.lower()
    assert "collision-free" in out or "device-partitioned" in out
    assert "drop" not in out  # the old data-loss warning is gone
