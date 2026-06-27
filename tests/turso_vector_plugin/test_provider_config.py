"""save_config round-trip and API-endpoint settings passthrough.

I1 fix: _load_settings now reads from config["memory"]["turso_vector"]
and save_config writes there. Tests updated to use the correct key path.
"""
import yaml

from plugins.memory.turso_vector import embedder as emb_mod
from plugins.memory.turso_vector.embedder import APIEmbedder


def test_save_config_roundtrips_into_load_settings(tmp_path, monkeypatch):
    # save_config → _load_settings sees the value (round-trip).
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from plugins.memory.turso_vector import TursoVectorMemoryProvider
    p = TursoVectorMemoryProvider()
    p.save_config({"top_k": 3, "embedding_backend": "local"},
                  hermes_home=str(tmp_path))

    settings = TursoVectorMemoryProvider()._load_settings()
    assert settings["top_k"] == 3
    assert settings["embedding_backend"] == "local"


def test_save_config_preserves_other_namespaces(tmp_path, monkeypatch):
    # save_config must not clobber unrelated config or sibling keys.
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(
        {"model": {"default": "x"},
         "memory": {"turso_vector": {"top_k": 1, "beta": 0.5}}}))
    from plugins.memory.turso_vector import TursoVectorMemoryProvider
    TursoVectorMemoryProvider().save_config({"top_k": 5}, hermes_home=str(tmp_path))

    data = yaml.safe_load((tmp_path / "config.yaml").read_text())
    assert data["model"]["default"] == "x"                           # unrelated ns preserved
    assert data["memory"]["turso_vector"]["beta"] == 0.5             # sibling key preserved
    assert data["memory"]["turso_vector"]["top_k"] == 5              # updated


def test_api_endpoint_settings_thread_through_to_embedder(tmp_path, monkeypatch):
    # embedding_api_base must survive _load_settings and reach the APIEmbedder.
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    # I1 fix: write under config["memory"]["turso_vector"] (where _load_settings reads).
    (tmp_path / "config.yaml").write_text(yaml.safe_dump({"memory": {"turso_vector": {
        "embedding_backend": "api",
        "embedding_dim": 3,
        "embedding_model": "text-embedding-3-small",
        "embedding_api_base": "https://api.example.com/v1",
    }}}))
    from plugins.memory.turso_vector import TursoVectorMemoryProvider
    settings = TursoVectorMemoryProvider()._load_settings()
    assert settings["embedding_api_base"] == "https://api.example.com/v1"

    emb = emb_mod.make_embedder(settings)
    assert isinstance(emb, APIEmbedder)
    assert emb.api_base == "https://api.example.com/v1"
    assert emb.model == "text-embedding-3-small"


# ---------------------------------------------------------------------------
# I1 REAL-PATH test: config["memory"]["turso_vector"] → settings → embedder
# ---------------------------------------------------------------------------

def test_config_routed_via_memory_key_produces_api_embedder(tmp_path, monkeypatch):
    """I1 real-path: config written under memory.turso_vector flows through
    _load_settings and make_embedder to produce a correctly-configured APIEmbedder.

    Only the HTTP call inside APIEmbedder.embed() is monkeypatched; make_embedder
    itself runs for real.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    # Write config under the key that _load_settings now reads (I1 fix).
    config = {
        "memory": {
            "turso_vector": {
                "embedding_backend": "api",
                "embedding_api_base": "https://x/v1",
                "embedding_model": "m",
                "embedding_dim": 128,
            }
        }
    }
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(config))

    from plugins.memory.turso_vector import TursoVectorMemoryProvider
    from plugins.memory.turso_vector.embedder import APIEmbedder, make_embedder

    p = TursoVectorMemoryProvider()
    settings = p._load_settings()

    # Real make_embedder path — not monkeypatched.
    embedder = make_embedder(settings)

    assert isinstance(embedder, APIEmbedder), (
        f"expected APIEmbedder, got {type(embedder).__name__}"
    )
    assert embedder.api_base == "https://x/v1", (
        f"api_base mismatch: {embedder.api_base!r}"
    )
    assert embedder.model == "m", f"model mismatch: {embedder.model!r}"
    assert embedder.dim == 128
