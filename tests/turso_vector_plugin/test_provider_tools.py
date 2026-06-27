# tests/turso_vector_plugin/test_provider_tools.py
import json

import pytest

from plugins.memory.turso_vector import embedder as emb_mod


class _FakeEmbedder:
    dim = 4
    def embed(self, text):  # deterministic, content-insensitive is fine here
        return [1.0, 0.0, 0.0, 0.0]


@pytest.fixture
def provider(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(emb_mod, "make_embedder", lambda cfg: _FakeEmbedder())
    # Config dim must match the fake embedder's dim (validated in initialize()).
    import yaml
    # I1 fix: config is now read from config["memory"]["turso_vector"].
    (tmp_path / "config.yaml").write_text(
        yaml.safe_dump({"memory": {"turso_vector": {"embedding_dim": 4}}}))
    from plugins.memory.turso_vector import TursoVectorMemoryProvider
    p = TursoVectorMemoryProvider()
    p.initialize("sess-1", hermes_home=str(tmp_path), platform="cli", cwd=str(tmp_path))
    return p


def test_schemas_expose_four_tools(provider):
    names = {s["name"] for s in provider.get_tool_schemas()}
    assert names == {"memory_report", "memory_remember", "memory_rate", "memory_contradict"}


def test_report_stores_correction(provider):
    out = json.loads(provider.handle_tool_call("memory_report", {
        "kind": "correction", "lesson": "Use run_tests.sh",
        "what_failed": "ran bare pytest", "what_worked": "scripts/run_tests.sh"}))
    assert "stored_id" in out
    assert provider._store.count() == 1


def test_rate_only_applies_to_retrieved(provider):
    mid = json.loads(provider.handle_tool_call(
        "memory_remember", {"text": "fact"}))["stored_id"]
    # Not retrieved yet -> rating is ignored.
    out = json.loads(provider.handle_tool_call("memory_rate", {
        "ratings": [{"id": mid, "score": 3}]}))
    assert out["rated"] == []
    # Mark as retrieved, then rating applies.
    provider._retrieved[mid] = {"id": mid}
    out2 = json.loads(provider.handle_tool_call("memory_rate", {
        "ratings": [{"id": mid, "score": 3}]}))
    assert out2["rated"] == [mid]


def test_contradict_deletes(provider):
    mid = json.loads(provider.handle_tool_call(
        "memory_remember", {"text": "wrong fact"}))["stored_id"]
    out = json.loads(provider.handle_tool_call("memory_contradict", {"id": mid}))
    assert out["deleted"] is True
    assert provider._store.count() == 0
