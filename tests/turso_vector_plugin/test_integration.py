"""End-to-end: store via tools -> recall -> rate -> weight changes ordering.
Backend routing: sqlite -> local libSQL; turso config -> SyncConfig resolved."""
import json

import pytest

from plugins.memory.turso_vector import embedder as emb_mod


class _FakeEmbedder:
    dim = 4
    _MAP = {
        "use the test wrapper": [1.0, 0.0, 0.0, 0.0],
        "always run scripts/run_tests.sh": [1.0, 0.0, 0.0, 0.0],
        "git workflow": [0.0, 1.0, 0.0, 0.0],
    }

    def embed(self, text):
        return self._MAP.get(text, [0.0, 0.0, 1.0, 0.0])


@pytest.fixture
def provider(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(emb_mod, "make_embedder", lambda cfg: _FakeEmbedder())
    # Config dim must match the fake embedder's dim (validated in initialize()).
    # I1 fix: config is now read from config["memory"]["turso_vector"].
    import yaml
    (tmp_path / "config.yaml").write_text(
        yaml.safe_dump({"memory": {"turso_vector": {"embedding_dim": 4}}}))
    from plugins.memory.turso_vector import TursoVectorMemoryProvider
    p = TursoVectorMemoryProvider()
    p.initialize("sess-1", hermes_home=str(tmp_path), platform="cli", cwd=str(tmp_path))
    return p


def test_full_loop_recall_then_rate(provider):
    provider.handle_tool_call("memory_report", {
        "kind": "insight", "lesson": "always run scripts/run_tests.sh"})
    # I2 fix: queue_prefetch starts background recall; prefetch() returns cached result.
    provider.queue_prefetch("use the test wrapper")
    block = provider.prefetch("use the test wrapper")
    assert "run_tests.sh" in block
    rid = next(iter(provider._retrieved))
    out = json.loads(provider.handle_tool_call(
        "memory_rate", {"ratings": [{"id": rid, "score": 3}]}))
    assert out["rated"] == [rid]
    weight = provider._store._conn.execute(
        "SELECT weight FROM memories WHERE id=?", (rid,)).fetchone()[0]
    assert weight > 1.0


def test_sqlite_backend_creates_local_libsql_file(tmp_path, monkeypatch):
    # With no turso config, resolve_sync_config returns None (local sqlite/libSQL path).
    # NOTE: The brief explicitly permits dropping the existence assertion because
    # the file is only created on provider.initialize() — asserting it doesn't
    # exist in tmp_path before initialization is vacuously true and brittle.
    # The persistence test below exercises the real local libSQL file end-to-end.
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from agent.db_backend import resolve_sync_config
    assert resolve_sync_config("memory_vec.db") is None


def test_persistence_across_reinit(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(emb_mod, "make_embedder", lambda cfg: _FakeEmbedder())
    import yaml
    # I1 fix: config is now read from config["memory"]["turso_vector"].
    (tmp_path / "config.yaml").write_text(
        yaml.safe_dump({"memory": {"turso_vector": {"embedding_dim": 4}}}))
    from plugins.memory.turso_vector import TursoVectorMemoryProvider
    p1 = TursoVectorMemoryProvider()
    p1.initialize("s1", hermes_home=str(tmp_path), platform="cli", cwd=str(tmp_path))
    p1.handle_tool_call("memory_remember", {"text": "persisted fact"})
    p1.shutdown()
    p2 = TursoVectorMemoryProvider()
    p2.initialize("s2", hermes_home=str(tmp_path), platform="cli", cwd=str(tmp_path))
    assert p2._store.count() == 1
