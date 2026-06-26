import json

import pytest

from plugins.memory.turso_memory import TursoMemoryProvider


class FakeEncoder:
    model_id = "fake/3"
    dim = 3

    def encode(self, texts):
        # deterministic 3-dim vector from char codes; good enough to rank
        out = []
        for t in texts:
            s = sum(ord(c) for c in t)
            out.append([float(s % 7), float(s % 5), float(s % 3)])
        return out


def _provider(tmp_path, monkeypatch, encoder=None):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    p = TursoMemoryProvider(config={"embedding": {"mode": "local"}})
    # inject a fake encoder so no fastembed/network is needed
    p._encoder = encoder if encoder is not None else FakeEncoder()
    p.initialize(session_id="s1")
    return p


def test_name_and_tool_schema(tmp_path, monkeypatch):
    p = _provider(tmp_path, monkeypatch)
    assert p.name == "turso_memory"
    names = {t["name"] for t in p.get_tool_schemas()}
    assert names == {"memory"}
    p.shutdown()


def test_remember_then_recall(tmp_path, monkeypatch):
    p = _provider(tmp_path, monkeypatch)
    r = json.loads(p.handle_tool_call("memory", {"action": "remember", "content": "the eagle lands at midnight"}))
    assert r["id"]
    out = json.loads(p.handle_tool_call("memory", {"action": "recall", "query": "eagle", "k": 5}))
    assert any("eagle" in m["content"] for m in out["results"])
    p.shutdown()


def test_forget_by_id(tmp_path, monkeypatch):
    p = _provider(tmp_path, monkeypatch)
    rid = json.loads(p.handle_tool_call("memory", {"action": "remember", "content": "delete me"}))["id"]
    r = json.loads(p.handle_tool_call("memory", {"action": "forget", "id": rid}))
    assert r["removed"] is True
    p.shutdown()


def test_prefetch_injects_relevant_memory(tmp_path, monkeypatch):
    p = _provider(tmp_path, monkeypatch)
    p.handle_tool_call("memory", {"action": "remember", "content": "user prefers dark mode"})
    block = p.prefetch("what theme does the user like", session_id="s1")
    assert "dark mode" in block
    p.shutdown()


def test_on_memory_write_mirrors_builtin(tmp_path, monkeypatch):
    p = _provider(tmp_path, monkeypatch)
    p.on_memory_write("add", "user", "lives in Cairo")
    out = json.loads(p.handle_tool_call("memory", {"action": "recall", "query": "Cairo"}))
    assert any("Cairo" in m["content"] for m in out["results"])
    p.shutdown()


def test_sync_turn_stores_nothing(tmp_path, monkeypatch):
    p = _provider(tmp_path, monkeypatch)
    p.sync_turn("hello", "hi there", session_id="s1")
    assert p._store.count() == 0
    p.shutdown()


def test_degrades_to_fts_when_encoder_unavailable(tmp_path, monkeypatch):
    class DeadEncoder:
        model_id = "dead/3"
        dim = 3                       # dim is known (model metadata); encode() fails
        def encode(self, texts):
            raise RuntimeError("offline")
    p = _provider(tmp_path, monkeypatch, encoder=DeadEncoder())
    p.handle_tool_call("memory", {"action": "remember", "content": "fallback keyword zebra"})
    out = json.loads(p.handle_tool_call("memory", {"action": "recall", "query": "zebra"}))
    assert any("zebra" in m["content"] for m in out["results"])  # FTS still works
    p.shutdown()
