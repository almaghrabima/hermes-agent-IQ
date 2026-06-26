import pytest

from plugins.memory.turso_memory.encoder import (
    ApiEncoder,
    EncoderUnavailable,
    get_encoder,
)


def test_get_encoder_api_mode_builds_api_encoder():
    enc = get_encoder({
        "mode": "api",
        "api": {"base_url": "https://x/v1", "api_key": "k", "model": "m", "dim": 8},
    })
    assert isinstance(enc, ApiEncoder)
    assert enc.dim == 8
    assert enc.model_id == "m"


def test_get_encoder_unknown_mode_raises():
    with pytest.raises(EncoderUnavailable):
        get_encoder({"mode": "nonsense"})


def test_api_encoder_posts_and_parses(monkeypatch):
    # Stub the HTTP call so no network is needed.
    captured = {}

    def fake_post(url, json, headers, timeout):
        captured["url"] = url
        captured["n"] = len(json["input"])

        class R:
            def raise_for_status(self): pass
            def json(self):
                return {"data": [{"embedding": [0.1, 0.2, 0.3]} for _ in json["input"]]}
        return R()

    import plugins.memory.turso_memory.encoder as enc_mod
    monkeypatch.setattr(enc_mod, "_http_post", fake_post)

    enc = ApiEncoder(base_url="https://x/v1", api_key="k", model="m", dim=3)
    vecs = enc.encode(["a", "b"])
    assert vecs == [[0.1, 0.2, 0.3], [0.1, 0.2, 0.3]]
    assert captured["n"] == 2
    assert captured["url"].endswith("/embeddings")

# ---------- FIX I1 — TURSO_MEMORY_EMBED_API_KEY env var takes precedence ----------

def test_get_encoder_api_key_from_env_var(monkeypatch):
    """TURSO_MEMORY_EMBED_API_KEY env var must be used as the API key."""
    import plugins.memory.turso_memory.encoder as enc_mod

    captured = {}

    def fake_post(url, json, headers, timeout):
        captured["auth"] = headers.get("Authorization", "")

        class R:
            def raise_for_status(self): pass
            def json(self):
                return {"data": [{"embedding": [0.1]} for _ in json["input"]]}
        return R()

    monkeypatch.setattr(enc_mod, "_http_post", fake_post)
    monkeypatch.setenv("TURSO_MEMORY_EMBED_API_KEY", "env-secret-key")

    # No api_key in config; env var must supply it
    enc = enc_mod.get_encoder({
        "mode": "api",
        "api": {"base_url": "https://x/v1", "model": "m", "dim": 1},
    })
    enc.encode(["test"])
    assert "env-secret-key" in captured["auth"], (
        f"Expected env var key in Authorization; got: {captured['auth']}"
    )


def test_get_encoder_api_env_key_overrides_config_key(monkeypatch):
    """Env var TURSO_MEMORY_EMBED_API_KEY takes precedence over config api_key."""
    import plugins.memory.turso_memory.encoder as enc_mod

    captured = {}

    def fake_post(url, json, headers, timeout):
        captured["auth"] = headers.get("Authorization", "")

        class R:
            def raise_for_status(self): pass
            def json(self):
                return {"data": [{"embedding": [0.1]} for _ in json["input"]]}
        return R()

    monkeypatch.setattr(enc_mod, "_http_post", fake_post)
    monkeypatch.setenv("TURSO_MEMORY_EMBED_API_KEY", "from-env")

    enc = enc_mod.get_encoder({
        "mode": "api",
        "api": {"base_url": "https://x/v1", "api_key": "from-config", "dim": 1},
    })
    enc.encode(["test"])
    assert "from-env" in captured["auth"], (
        "Env var did not override config api_key"
    )
    assert "from-config" not in captured["auth"]


def test_get_encoder_api_raises_without_env_and_config_key(monkeypatch):
    """Without env var and without config api_key, get_encoder raises EncoderUnavailable."""
    from plugins.memory.turso_memory.encoder import EncoderUnavailable, get_encoder
    import pytest
    monkeypatch.delenv("TURSO_MEMORY_EMBED_API_KEY", raising=False)
    with pytest.raises(EncoderUnavailable):
        get_encoder({"mode": "api", "api": {"base_url": "https://x/v1", "dim": 1}})
