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
