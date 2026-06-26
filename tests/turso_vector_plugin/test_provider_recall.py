"""Provider recall with a deterministic fake embedder (no model download)."""
import os

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

    block = provider.prefetch("how do I run tests", session_id="sess-1")
    assert "run_tests.sh" in block
    assert "Pasta" not in block
    # The retrieved memory is recorded in the ledger for the rating loop.
    assert len(provider._retrieved) >= 1


def test_disabled_provider_is_inert(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from plugins.memory.turso_vector import TursoVectorMemoryProvider
    p = TursoVectorMemoryProvider()
    # Never initialized -> disabled -> no crash, empty recall.
    assert p.prefetch("anything") == ""
