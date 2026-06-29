"""Tests for the Turso/Temporal/Fast-RLM setup-wizard flows."""

import hermes_cli.setup as setup_mod


def test_ensure_optional_dep_already_present(monkeypatch):
    monkeypatch.setattr("tools.lazy_deps.is_available", lambda feature: True)
    called = []
    monkeypatch.setattr("tools.lazy_deps.ensure", lambda *a, **k: called.append(a))
    assert setup_mod._ensure_optional_dep("tool.temporal", "Temporal SDK") is True
    assert called == []  # no install attempted when already available


def test_ensure_optional_dep_installs_then_succeeds(monkeypatch):
    monkeypatch.setattr("tools.lazy_deps.is_available", lambda feature: False)
    monkeypatch.setattr("tools.lazy_deps.ensure", lambda feature, prompt=True: None)
    assert setup_mod._ensure_optional_dep("tool.temporal", "Temporal SDK") is True


def test_ensure_optional_dep_failure_prints_manual_command(monkeypatch, capsys):
    monkeypatch.setattr("tools.lazy_deps.is_available", lambda feature: False)

    def _boom(feature, prompt=True):
        raise RuntimeError("network down")

    monkeypatch.setattr("tools.lazy_deps.ensure", _boom)
    monkeypatch.setattr("tools.lazy_deps.feature_install_command",
                        lambda feature: "uv pip install 'temporalio==1.29.0'")
    assert setup_mod._ensure_optional_dep("tool.temporal", "Temporal SDK") is False
    out = capsys.readouterr().out
    assert "uv pip install 'temporalio==1.29.0'" in out


# =============================================================================
# Turso tests
# =============================================================================

from hermes_cli.setup import setup_turso


def _patch_no_install(monkeypatch):
    monkeypatch.setattr("hermes_cli.setup._ensure_optional_dep", lambda *a, **k: True)


def test_setup_turso_enables_backend_and_saves_token(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _patch_no_install(monkeypatch)

    saved_env = {}
    monkeypatch.setattr("hermes_cli.setup.save_env_value",
                        lambda k, v: saved_env.__setitem__(k, v))
    # prompt_choice: index 1 == "Turso (libSQL cloud sync)"
    monkeypatch.setattr("hermes_cli.setup.prompt_choice", lambda *a, **k: 1)
    # text prompts in order: sync_url, sync_interval
    answers = iter(["libsql://db-org.turso.io", "30"])
    monkeypatch.setattr("hermes_cli.setup.prompt",
                        lambda q, default=None, password=False: (
                            "tok-secret" if password else next(answers)))
    # decline the connectivity check
    monkeypatch.setattr("hermes_cli.setup.prompt_yes_no", lambda *a, **k: False)

    config = {}
    setup_turso(config)

    assert config["database"]["backend"] == "turso"
    assert config["database"]["turso"]["sync_url"] == "libsql://db-org.turso.io"
    assert config["database"]["turso"]["sync_interval"] == 30
    assert saved_env["TURSO_AUTH_TOKEN"] == "tok-secret"


def test_setup_turso_local_sets_sqlite(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _patch_no_install(monkeypatch)
    monkeypatch.setattr("hermes_cli.setup.prompt_choice", lambda *a, **k: 0)  # Local
    monkeypatch.setattr("hermes_cli.setup.save_env_value", lambda k, v: None)

    config = {"database": {"backend": "turso", "turso": {"sync_url": "libsql://x"}}}
    setup_turso(config)

    assert config["database"]["backend"] == "sqlite"
    # turso sub-block preserved so switching back is lossless
    assert config["database"]["turso"]["sync_url"] == "libsql://x"


def test_setup_turso_empty_url_aborts(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _patch_no_install(monkeypatch)
    monkeypatch.setattr("hermes_cli.setup.prompt_choice", lambda *a, **k: 1)  # Turso
    # empty sync_url -> abort without mutating backend
    monkeypatch.setattr("hermes_cli.setup.prompt",
                        lambda q, default=None, password=False: "")
    monkeypatch.setattr("hermes_cli.setup.save_env_value", lambda k, v: None)

    config = {}
    setup_turso(config)
    assert config.get("database", {}).get("backend") != "turso"
