import json

import pytest
pytest.importorskip("libsql")  # skip whole module when libsql is absent

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
    # I2: must call queue_prefetch() first; prefetch() returns the cached result.
    p = _provider(tmp_path, monkeypatch)
    p.handle_tool_call("memory", {"action": "remember", "content": "user prefers dark mode"})
    p.queue_prefetch("what theme does the user like", session_id="s1")
    block = p.prefetch("what theme does the user like", session_id="s1")
    assert "dark mode" in block
    p.shutdown()


def test_on_memory_write_mirrors_builtin(tmp_path, monkeypatch):
    p = _provider(tmp_path, monkeypatch)
    p.on_memory_write("add", "user", "lives in Cairo")
    out = json.loads(p.handle_tool_call("memory", {"action": "recall", "query": "Cairo"}))
    assert any("Cairo" in m["content"] for m in out["results"])
    p.shutdown()


def test_on_memory_write_remove_path(tmp_path, monkeypatch):
    p = _provider(tmp_path, monkeypatch)
    p.on_memory_write("add", "user", "temporary fact abc")
    p.on_memory_write("remove", "user", "temporary fact abc")
    out = json.loads(p.handle_tool_call("memory", {"action": "recall", "query": "temporary fact abc"}))
    assert not any("temporary fact abc" in m["content"] for m in out["results"])
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

# ---------- FIX I1 — get_config_schema marks the API key as secret ----------

def test_get_config_schema_has_api_key_secret(tmp_path, monkeypatch):
    """get_config_schema() must include the embedding API key marked secret with env_var."""
    p = _provider(tmp_path, monkeypatch)
    schema = p.get_config_schema()
    assert isinstance(schema, list) and schema, "get_config_schema() returned empty"
    secret_keys = [f for f in schema if f.get("secret")]
    assert secret_keys, "No secret fields in get_config_schema()"
    env_vars = {f.get("env_var") for f in secret_keys}
    assert "TURSO_MEMORY_EMBED_API_KEY" in env_vars, (
        f"TURSO_MEMORY_EMBED_API_KEY not in env_vars of secret fields: {env_vars}"
    )
    p.shutdown()


# ---------- FIX I2 — prefetch() must not invoke encoder/DB synchronously ----------

def test_prefetch_alone_does_not_call_encoder(tmp_path, monkeypatch):
    """prefetch() without a prior queue_prefetch() must NOT call the encoder (hot path)."""
    class TrackingEncoder:
        model_id = "tracking/3"
        dim = 3
        calls = 0

        def encode(self, texts):
            TrackingEncoder.calls += 1
            return [[float(sum(ord(c) for c in t) % 7)] * 3 for t in texts]

    enc = TrackingEncoder()
    p = _provider(tmp_path, monkeypatch, encoder=enc)
    calls_before = TrackingEncoder.calls
    # prefetch() without queue_prefetch() — must return "" and NOT touch the encoder
    result = p.prefetch("query that nobody pre-fetched", session_id="fresh")
    assert result == "", f"expected '' without queue_prefetch, got: {result!r}"
    assert TrackingEncoder.calls == calls_before, (
        "prefetch() called the encoder synchronously on the hot path"
    )
    p.shutdown()


def test_queue_prefetch_then_prefetch_returns_block(tmp_path, monkeypatch):
    """queue_prefetch() computes in background; prefetch() returns the cached block."""
    p = _provider(tmp_path, monkeypatch)
    p.handle_tool_call("memory", {"action": "remember", "content": "user prefers vim"})
    p.queue_prefetch("what editor does the user prefer", session_id="s2")
    block = p.prefetch("what editor does the user prefer", session_id="s2")
    assert "vim" in block, f"Expected 'vim' in prefetch block, got: {block!r}"
    p.shutdown()
