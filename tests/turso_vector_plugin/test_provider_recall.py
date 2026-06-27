"""Provider recall with a deterministic fake embedder (no model download)."""
import threading

import pytest

from plugins.memory.turso_vector import embedder as emb_mod


class _FakeEmbedder:
    dim = 4
    _MAP = {
        "python testing": [1.0, 0.0, 0.0, 0.0],
        "how do I run tests": [0.99, 0.01, 0.0, 0.0],
        "cooking pasta": [0.0, 0.0, 0.0, 1.0],
    }
    def embed(self, text):
        return self._MAP.get(text, [0.25, 0.25, 0.25, 0.25])


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


def test_recall_returns_semantically_nearest(provider):
    # Store two memories via the store directly through the provider helper.
    provider._store.insert(kind="insight", project=None, cwd=None,
        text="To run tests use scripts/run_tests.sh",
        what_failed=None, what_worked=None,
        embedding=provider._embedder.embed("python testing"),
        created_at="2026-06-27T00:00:00+00:00", source_session="s0")
    provider._store.insert(kind="insight", project=None, cwd=None,
        text="Pasta needs salted boiling water",
        what_failed=None, what_worked=None,
        embedding=provider._embedder.embed("cooking pasta"),
        created_at="2026-06-27T00:00:00+00:00", source_session="s0")

    # I2 fix: queue_prefetch starts background work; prefetch() returns cached result.
    provider.queue_prefetch("how do I run tests", session_id="sess-1")
    block = provider.prefetch("how do I run tests", session_id="sess-1")
    assert "run_tests.sh" in block
    assert "Pasta" not in block
    # The retrieved memory is recorded in the ledger for the rating loop.
    assert len(provider._retrieved) >= 1


def test_recall_block_surfaces_memory_id(provider):
    # F1: the recalled block must prefix each line with the integer id so the
    # model can reference it in memory_rate / memory_contradict.
    mid = provider._store.insert(kind="correction", project=None, cwd=None,
        text="To run tests use scripts/run_tests.sh",
        what_failed="ran bare pytest", what_worked="scripts/run_tests.sh",
        embedding=provider._embedder.embed("python testing"),
        created_at="2026-06-27T00:00:00+00:00", source_session="s0")

    provider.queue_prefetch("how do I run tests", session_id="sess-1")
    block = provider.prefetch("how do I run tests", session_id="sess-1")
    assert f"[#{mid}]" in block
    assert f"- [#{mid}][correction]" in block


def test_disabled_provider_is_inert(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from plugins.memory.turso_vector import TursoVectorMemoryProvider
    p = TursoVectorMemoryProvider()
    # Never initialized -> disabled -> no crash, empty recall.
    assert p.prefetch("anything") == ""


# ---------------------------------------------------------------------------
# I2 REAL-PATH test: prefetch() must NOT call embed() on the caller's thread
# ---------------------------------------------------------------------------

class _ThreadRecordingEmbedder:
    """Records the thread id of every embed() call."""
    dim = 4

    def __init__(self):
        self.embed_threads: list = []

    def embed(self, text):
        self.embed_threads.append(threading.get_ident())
        return [1.0, 0.0, 0.0, 0.0]


def test_prefetch_does_not_embed_on_main_thread(tmp_path, monkeypatch):
    """queue_prefetch() starts background embedding; prefetch() never calls embed()."""
    main_thread = threading.get_ident()

    recording_embedder = _ThreadRecordingEmbedder()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(emb_mod, "make_embedder", lambda cfg: recording_embedder)

    import yaml
    (tmp_path / "config.yaml").write_text(
        yaml.safe_dump({"memory": {"turso_vector": {"embedding_dim": 4}}}))

    from plugins.memory.turso_vector import TursoVectorMemoryProvider
    p = TursoVectorMemoryProvider()
    p.initialize("sess-q", hermes_home=str(tmp_path), platform="cli", cwd=str(tmp_path))

    # Insert a memory so there's something to recall.
    p._store.insert(kind="insight", project=None, cwd=None,
        text="background recall fact",
        what_failed=None, what_worked=None,
        embedding=[1.0, 0.0, 0.0, 0.0],
        created_at="2026-06-27T00:00:00+00:00", source_session="s0")

    # Clear the embed record accumulated during warmup (runs on executor thread).
    recording_embedder.embed_threads.clear()

    # queue_prefetch() starts background work; prefetch() returns the cached result.
    p.queue_prefetch("test query", session_id="sess-q")
    _result = p.prefetch("test query", session_id="sess-q")

    # embed() must have been called (at least by the background prefetch).
    assert len(recording_embedder.embed_threads) >= 1, "embed() was never called"

    # NONE of the embed() calls may have happened on the main thread.
    for tid in recording_embedder.embed_threads:
        assert tid != main_thread, (
            f"embed() was called on the main thread (tid={tid}); "
            "prefetch must delegate all embedding to a background thread"
        )

    p.shutdown()
