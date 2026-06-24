import importlib

import tools.rlm_tool as rlm_tool


def test_check_rlm_available_true_when_deno_and_fastrlm(monkeypatch):
    monkeypatch.setattr(rlm_tool, "_deno_available", lambda: True)
    monkeypatch.setattr(rlm_tool, "_fast_rlm_available", lambda: True)
    assert rlm_tool.check_rlm_available() is True


def test_check_rlm_available_false_when_no_deno(monkeypatch):
    monkeypatch.setattr(rlm_tool, "_deno_available", lambda: False)
    monkeypatch.setattr(rlm_tool, "_fast_rlm_available", lambda: True)
    assert rlm_tool.check_rlm_available() is False


def test_check_rlm_available_false_when_no_fastrlm(monkeypatch):
    monkeypatch.setattr(rlm_tool, "_deno_available", lambda: True)
    monkeypatch.setattr(rlm_tool, "_fast_rlm_available", lambda: False)
    assert rlm_tool.check_rlm_available() is False


def test_deno_available_uses_which(monkeypatch):
    monkeypatch.setattr(rlm_tool.shutil, "which", lambda name: "/usr/bin/deno" if name == "deno" else None)
    assert rlm_tool._deno_available() is True
    monkeypatch.setattr(rlm_tool.shutil, "which", lambda name: None)
    assert rlm_tool._deno_available() is False
