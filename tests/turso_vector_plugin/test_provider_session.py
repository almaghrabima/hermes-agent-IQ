# tests/turso_vector_plugin/test_provider_session.py
import pytest

from plugins.memory.turso_vector import embedder as emb_mod


class _FakeEmbedder:
    dim = 4
    def embed(self, text):
        return [1.0, 0.0, 0.0, 0.0]


@pytest.fixture
def provider(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(emb_mod, "make_embedder", lambda cfg: _FakeEmbedder())
    from plugins.memory.turso_vector import TursoVectorMemoryProvider
    p = TursoVectorMemoryProvider()
    p.initialize("sess-1", hermes_home=str(tmp_path), platform="cli", cwd=str(tmp_path))
    return p


def test_system_prompt_block_is_static_and_nonempty(provider):
    a = provider.system_prompt_block()
    b = provider.system_prompt_block()
    assert a == b and a.strip()              # byte-stable, non-empty
    assert "memory_report" in a


def test_session_end_decays_and_prunes_touched(provider):
    # An old, low-weight, retrieved memory should be pruned by the decay sweep.
    mid = provider._store.insert(kind="insight", project=None, cwd=None,
        text="stale", what_failed=None, what_worked=None,
        embedding=[1, 0, 0, 0], created_at="2026-01-01T00:00:00+00:00",
        source_session="s0", weight=0.2)
    provider._retrieved[mid] = {"id": mid}
    provider._settings["decay_rate"] = 0.9
    provider._settings["weight_floor"] = 0.15
    provider.on_session_end([])
    assert provider._store.count() == 0
    assert provider._retrieved == {}


def test_config_schema_has_expected_keys(provider):
    keys = {f["key"] for f in provider.get_config_schema()}
    assert {"embedding_backend", "embedding_model", "top_k", "auto_extract"} <= keys


def test_shutdown_is_safe(provider):
    provider.shutdown()   # must not raise
