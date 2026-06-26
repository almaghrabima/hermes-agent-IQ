"""Tests for TursoMemoryProvider.initialize() built-in reconcile and
on_session_switch(reset=True) re-reconcile.

Built-in format confirmed (Step 1): ENTRY_DELIMITER = "\\n§\\n" (from
tools/memory_tool.py line 59).  Entries are stored as plain text joined by
that three-character sequence.  The brief's seed assumed a leading-§ prefix
format; the real format is delimiter-between.  This test and the reconcile
parser both use "\\n§\\n".
"""
import json
from pathlib import Path

import pytest

from plugins.memory.turso_memory import TursoMemoryProvider
from tests.plugins.turso_memory.test_provider import FakeEncoder  # reuse fake

# The real delimiter from tools/memory_tool.py
ENTRY_DELIMITER = "\n§\n"


def test_initialize_reconciles_existing_builtin_entries(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    mem_dir = tmp_path / "memories"
    mem_dir.mkdir(parents=True)
    # Built-in file memory — entries joined by the real ENTRY_DELIMITER ("\n§\n")
    (mem_dir / "MEMORY.md").write_text(
        "user is a pilot" + ENTRY_DELIMITER + "project hermes ships Friday",
        encoding="utf-8",
    )

    p = TursoMemoryProvider(config={"embedding": {"mode": "local"}})
    p._encoder = FakeEncoder()
    p.initialize(session_id="s1")  # reconcile should import the two entries

    out = json.loads(p.handle_tool_call("memory", {"action": "recall", "query": "pilot"}))
    assert any("pilot" in m["content"] for m in out["results"])
    assert p._store.count() == 2
    p.shutdown()


def test_initialize_reconciles_user_md_entries(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    mem_dir = tmp_path / "memories"
    mem_dir.mkdir(parents=True)
    (mem_dir / "USER.md").write_text(
        "user prefers dark mode" + ENTRY_DELIMITER + "user is based in Cairo",
        encoding="utf-8",
    )

    p = TursoMemoryProvider(config={"embedding": {"mode": "local"}})
    p._encoder = FakeEncoder()
    p.initialize(session_id="s1")

    out = json.loads(p.handle_tool_call("memory", {"action": "recall", "query": "Cairo"}))
    assert any("Cairo" in m["content"] for m in out["results"])
    assert p._store.count() == 2
    p.shutdown()


def test_initialize_reconcile_idempotent(tmp_path, monkeypatch):
    """Calling initialize twice (or adding same entries) must not duplicate rows."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    mem_dir = tmp_path / "memories"
    mem_dir.mkdir(parents=True)
    (mem_dir / "MEMORY.md").write_text("unique fact omega", encoding="utf-8")

    p = TursoMemoryProvider(config={"embedding": {"mode": "local"}})
    p._encoder = FakeEncoder()
    p.initialize(session_id="s1")
    assert p._store.count() == 1

    # Second initialize — same entries, no duplicates
    p.initialize(session_id="s2")
    assert p._store.count() == 1
    p.shutdown()


def test_on_session_switch_reset_reconciles(tmp_path, monkeypatch):
    """on_session_switch(reset=True) must re-run the reconcile so new entries
    added to MEMORY.md between sessions are picked up."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    mem_dir = tmp_path / "memories"
    mem_dir.mkdir(parents=True)
    (mem_dir / "MEMORY.md").write_text("original entry", encoding="utf-8")

    p = TursoMemoryProvider(config={"embedding": {"mode": "local"}})
    p._encoder = FakeEncoder()
    p.initialize(session_id="s1")
    assert p._store.count() == 1

    # Simulate a new entry written to MEMORY.md between sessions
    (mem_dir / "MEMORY.md").write_text(
        "original entry" + ENTRY_DELIMITER + "new entry added mid-session",
        encoding="utf-8",
    )

    p.on_session_switch("s2", reset=True)
    assert p._store.count() == 2
    out = json.loads(p.handle_tool_call("memory", {"action": "recall", "query": "new entry"}))
    assert any("new entry" in m["content"] for m in out["results"])
    p.shutdown()


def test_on_session_switch_no_reset_skips_reconcile(tmp_path, monkeypatch):
    """on_session_switch(reset=False) must NOT reconcile (normal continuation)."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    mem_dir = tmp_path / "memories"
    mem_dir.mkdir(parents=True)
    (mem_dir / "MEMORY.md").write_text("original entry", encoding="utf-8")

    p = TursoMemoryProvider(config={"embedding": {"mode": "local"}})
    p._encoder = FakeEncoder()
    p.initialize(session_id="s1")
    assert p._store.count() == 1

    # Add a new entry to MEMORY.md
    (mem_dir / "MEMORY.md").write_text(
        "original entry" + ENTRY_DELIMITER + "entry that should not appear",
        encoding="utf-8",
    )

    # reset=False — no reconcile
    p.on_session_switch("s2", reset=False)
    assert p._store.count() == 1  # still just the original
    p.shutdown()


def test_reconcile_drops_removed_builtin_entries(tmp_path, monkeypatch):
    """Entries removed from MEMORY.md must be purged from the store on reconcile."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    mem_dir = tmp_path / "memories"
    mem_dir.mkdir(parents=True)
    (mem_dir / "MEMORY.md").write_text(
        "keep this" + ENTRY_DELIMITER + "remove this",
        encoding="utf-8",
    )

    p = TursoMemoryProvider(config={"embedding": {"mode": "local"}})
    p._encoder = FakeEncoder()
    p.initialize(session_id="s1")
    assert p._store.count() == 2

    # Remove one entry from MEMORY.md
    (mem_dir / "MEMORY.md").write_text("keep this", encoding="utf-8")
    p.on_session_switch("s2", reset=True)

    assert p._store.count() == 1
    out = json.loads(p.handle_tool_call("memory", {"action": "recall", "query": "remove this"}))
    assert not any("remove this" in m["content"] for m in out["results"])
    p.shutdown()
