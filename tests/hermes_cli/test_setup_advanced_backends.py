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


# =============================================================================
# Temporal tests
# =============================================================================

from hermes_cli.setup import setup_temporal


def test_setup_temporal_dev_server_enable(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _patch_no_install(monkeypatch)
    # yes_no in order: enable? -> True, use dev server? -> True
    yn = iter([True, True])
    monkeypatch.setattr("hermes_cli.setup.prompt_yes_no", lambda *a, **k: next(yn))
    monkeypatch.setattr("hermes_cli.setup.save_env_value", lambda k, v: None)

    config = {}
    setup_temporal(config)

    assert config["temporal"]["enabled"] is True
    assert config["temporal"]["dev_server"] is True


def test_setup_temporal_external_server_writes_target(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _patch_no_install(monkeypatch)
    # enable? True; use dev server? False; tls? False
    yn = iter([True, False, False])
    monkeypatch.setattr("hermes_cli.setup.prompt_yes_no", lambda *a, **k: next(yn))
    # prompts: target, namespace, api_key(password)
    answers = iter(["temporal.example.com:7233", "prod"])
    monkeypatch.setattr("hermes_cli.setup.prompt",
                        lambda q, default=None, password=False: (
                            "" if password else next(answers)))
    monkeypatch.setattr("hermes_cli.setup.save_env_value", lambda k, v: None)

    config = {}
    setup_temporal(config)

    assert config["temporal"]["enabled"] is True
    assert config["temporal"]["dev_server"] is False
    assert config["temporal"]["target"] == "temporal.example.com:7233"
    assert config["temporal"]["namespace"] == "prod"


def test_setup_temporal_disable(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("hermes_cli.setup.prompt_yes_no", lambda *a, **k: False)

    config = {"temporal": {"enabled": True, "target": "x:7233"}}
    setup_temporal(config)

    assert config["temporal"]["enabled"] is False
    assert config["temporal"]["target"] == "x:7233"  # other keys preserved


# =============================================================================
# RLM tests
# =============================================================================

from hermes_cli.setup import setup_rlm


def test_setup_rlm_enables_toolset_cli(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _patch_no_install(monkeypatch)
    monkeypatch.setattr("shutil.which", lambda name: "/usr/local/bin/deno")
    # prompt_choice: 0 == "CLI only (recommended)"
    monkeypatch.setattr("hermes_cli.setup.prompt_choice", lambda *a, **k: 0)
    # decline advanced settings
    monkeypatch.setattr("hermes_cli.setup.prompt_yes_no", lambda *a, **k: False)

    config = {}
    setup_rlm(config)

    assert "rlm" in config["platform_toolsets"]["cli"]
    assert "rlm" not in config  # no rlm: block written when advanced declined


def test_setup_rlm_preserves_existing_toolsets(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _patch_no_install(monkeypatch)
    monkeypatch.setattr("shutil.which", lambda name: "/usr/local/bin/deno")
    monkeypatch.setattr("hermes_cli.setup.prompt_choice", lambda *a, **k: 0)
    monkeypatch.setattr("hermes_cli.setup.prompt_yes_no", lambda *a, **k: False)

    config = {"platform_toolsets": {"cli": ["web", "file"]}}
    setup_rlm(config)

    assert set(config["platform_toolsets"]["cli"]) == {"web", "file", "rlm"}


# =============================================================================
# Task 5: Wizard wiring tests
# =============================================================================

import argparse


def test_setup_parser_accepts_advanced_sections():
    from hermes_cli.subcommands.setup import build_setup_parser
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    build_setup_parser(sub, cmd_setup=lambda args: None)
    for section in ("turso", "temporal", "rlm"):
        args = parser.parse_args(["setup", section])
        assert args.section == section


def test_advanced_sections_map_to_callables():
    from hermes_cli.setup import (
        ADVANCED_SETUP_SECTIONS, setup_turso, setup_temporal, setup_rlm,
    )
    by_key = {key: func for key, _label, func in ADVANCED_SETUP_SECTIONS}
    assert by_key["turso"] is setup_turso
    assert by_key["temporal"] is setup_temporal
    assert by_key["rlm"] is setup_rlm


def test_section_dispatch_runs_turso(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    import hermes_cli.setup as s

    called = []
    monkeypatch.setattr(s, "setup_turso", lambda config: called.append("turso"))
    monkeypatch.setattr(s, "save_config", lambda *a, **k: None)
    monkeypatch.setattr(s, "is_interactive_stdin", lambda: True)
    # rebuild ADVANCED_SETUP_SECTIONS so it references the patched setup_turso
    monkeypatch.setattr(
        s, "ADVANCED_SETUP_SECTIONS",
        [("turso", "Turso (libSQL sync backend)", s.setup_turso),
         ("temporal", "Temporal (durable execution)", s.setup_temporal),
         ("rlm", "Fast-RLM (recursive LM toolset)", s.setup_rlm)],
    )

    args = argparse.Namespace(
        section="turso", reset=False, reconfigure=False, quick=False,
        portal=False, non_interactive=False,
    )
    s.run_setup_wizard(args)
    assert called == ["turso"]


# =============================================================================
# Task 6: Configurable toolsets
# =============================================================================

def test_rlm_in_configurable_toolsets():
    from hermes_cli.tools_config import CONFIGURABLE_TOOLSETS
    keys = {ts_key for ts_key, _label, _desc in CONFIGURABLE_TOOLSETS}
    assert "rlm" in keys
