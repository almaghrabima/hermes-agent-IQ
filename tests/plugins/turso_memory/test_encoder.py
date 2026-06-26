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
