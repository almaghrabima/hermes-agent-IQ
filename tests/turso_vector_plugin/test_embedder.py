import pytest

from plugins.memory.turso_vector import embedder as e


def test_make_embedder_defaults_to_local():
    emb = e.make_embedder({})
    assert isinstance(emb, e.LocalEmbedder)


def test_make_embedder_rejects_unknown_backend():
    with pytest.raises(ValueError):
        e.make_embedder({"embedding_backend": "nope"})


def test_api_embedder_calls_endpoint_and_returns_vector(monkeypatch):
    captured = {}

    class _Resp:
        def raise_for_status(self): pass
        def json(self): return {"data": [{"embedding": [0.1, 0.2, 0.3]}]}

    def _fake_post(url, **kw):
        captured["url"] = url
        captured["json"] = kw.get("json")
        return _Resp()

    monkeypatch.setenv("TURSO_VECTOR_EMBED_API_KEY", "k")
    import requests
    monkeypatch.setattr(requests, "post", _fake_post)

    emb = e.make_embedder({
        "embedding_backend": "api",
        "embedding_dim": 3,
        "embedding_model": "text-embedding-3-small",
        "embedding_api_base": "https://api.example.com/v1",
    })
    vec = emb.embed("hello")
    assert vec == [0.1, 0.2, 0.3]
    assert emb.dim == 3
    assert captured["json"]["input"] == "hello"
