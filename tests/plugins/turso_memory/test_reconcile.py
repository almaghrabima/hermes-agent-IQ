"""Tests for TursoMemoryProvider.initialize() built-in reconcile and
on_session_switch(reset=True) re-reconcile.

Built-in format confirmed (Step 1): ENTRY_DELIMITER = "\\n§\\n" (from
tools/memory_tool.py line 59).  Entries are stored as plain text joined by
that three-character sequence.  The brief's seed assumed a leading-§ prefix
format; the real format is delimiter-between.  This test and the reconcile
parser both use "\\n§\\n".
"""
import json


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


# ---------- FIX I1b — reconcile must NOT call encoder at init ----------

def test_reconcile_does_not_call_encoder(tmp_path, monkeypatch):
    """If encoder.encode raises, initialize() must NOT raise and the entry
    must still be FTS-recallable (reconcile inserts with embedding=None)."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    mem_dir = tmp_path / "memories"
    mem_dir.mkdir(parents=True)
    (mem_dir / "MEMORY.md").write_text("reconcile should not embed this", encoding="utf-8")

    class ExplodingEncoder:
        model_id = "exploding/3"
        dim = 3
        def encode(self, texts):
            raise RuntimeError("encoder must not be called during reconcile")

    p = TursoMemoryProvider(config={"embedding": {"mode": "local"}})
    p._encoder = ExplodingEncoder()
    p.initialize(session_id="s1")   # must NOT raise

    # Entry must be FTS-recallable even without an embedding
    ids = p._store.fts_search("reconcile")
    assert ids, "entry was not inserted by reconcile"
    p.shutdown()


def test_reconcile_skips_existing_entries(tmp_path, monkeypatch):
    """If an entry already exists for a source_key, reconcile must skip it
    (no upsert, no embed call for that entry)."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    mem_dir = tmp_path / "memories"
    mem_dir.mkdir(parents=True)
    (mem_dir / "MEMORY.md").write_text("stable fact", encoding="utf-8")

    class CountingEncoder:
        model_id = "counting/3"
        dim = 3
        calls = 0
        def encode(self, texts):
            self.calls += len(texts)
            return [[1.0, 0.0, 0.0]] * len(texts)

    enc = CountingEncoder()
    p = TursoMemoryProvider(config={"embedding": {"mode": "local"}})
    p._encoder = enc
    p.initialize(session_id="s1")
    calls_after_first_init = enc.calls   # may be 0 if reconcile skips embed

    # Second initialize — entry already exists; reconcile must not re-embed it
    p.initialize(session_id="s2")
    # Encoder must NOT have been called again for the pre-existing entry
    assert enc.calls == calls_after_first_init, (
        f"encoder was called again on second reconcile: "
        f"{enc.calls} total vs {calls_after_first_init} after first init"
    )
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

# ---------- FIX I3 — reconcile must not purge builtins when syncing ----------

def test_reconcile_does_not_purge_builtins_when_syncing(tmp_path, monkeypatch):
    """With sync active, reconcile must NOT delete a builtin row absent from local .md.

    Each device has its own .md files; Device B must not purge Device A's mirrored
    builtins that aren't in B's local file — they are valid rows on the shared replica.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    mem_dir = tmp_path / "memories"
    mem_dir.mkdir(parents=True)

    # Both entries start in the .md file
    (mem_dir / "MEMORY.md").write_text(
        "device-a-entry" + ENTRY_DELIMITER + "device-b-entry",
        encoding="utf-8",
    )

    p = TursoMemoryProvider(config={"embedding": {"mode": "local"}})
    p._encoder = FakeEncoder()
    p.initialize(session_id="s1")
    assert p._store.count() == 2

    # Simulate Device B: its .md file only has its own entry
    (mem_dir / "MEMORY.md").write_text("device-b-entry", encoding="utf-8")

    # Simulate sync being active — mark the store as synced
    from agent.db_backend import SyncConfig
    fake_sync = SyncConfig(sync_url="libsql://fake.turso.io", auth_token="tok")
    p._store._sync = fake_sync

    # Reconcile with sync active: device-a-entry must NOT be purged
    p.on_session_switch("s2", reset=True)
    assert p._store.count() == 2, (
        "Sync-mode reconcile must not purge cross-device builtins"
    )
    p.shutdown()


def test_reconcile_purges_builtins_when_not_syncing(tmp_path, monkeypatch):
    """Without sync (local-only mode), reconcile MUST purge builtins missing from local .md."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    mem_dir = tmp_path / "memories"
    mem_dir.mkdir(parents=True)
    (mem_dir / "MEMORY.md").write_text(
        "keep this" + ENTRY_DELIMITER + "will be removed",
        encoding="utf-8",
    )

    p = TursoMemoryProvider(config={"embedding": {"mode": "local"}})
    p._encoder = FakeEncoder()
    p.initialize(session_id="s1")
    assert p._store.count() == 2
    assert p._store._sync is None   # no sync configured

    # Remove one entry — local-only mode must purge it on next reconcile
    (mem_dir / "MEMORY.md").write_text("keep this", encoding="utf-8")
    p.on_session_switch("s2", reset=True)
    assert p._store.count() == 1, "Non-sync reconcile must purge missing builtins"
    p.shutdown()
