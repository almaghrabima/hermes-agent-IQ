"""F3: embedding-dim validation in initialize() surfaces loud, actionable errors."""
import logging

import yaml

from plugins.memory.turso_vector import embedder as emb_mod


def _mk(dim):
    class _Fake:
        def __init__(self):
            self.dim = dim
        def embed(self, text):
            return [1.0] + [0.0] * (dim - 1)
    return _Fake()


def _write_dim(tmp_path, dim):
    # I1 fix: _load_settings reads from config["memory"]["turso_vector"].
    (tmp_path / "config.yaml").write_text(
        yaml.safe_dump({"memory": {"turso_vector": {"embedding_dim": dim}}}))


def test_configured_dim_mismatch_disables_with_error(tmp_path, monkeypatch, caplog):
    # Config asks for dim 8 but the embedder produces dim 4 -> loud error + disabled.
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(emb_mod, "make_embedder", lambda cfg: _mk(4))
    _write_dim(tmp_path, 8)
    from plugins.memory.turso_vector import TursoVectorMemoryProvider
    p = TursoVectorMemoryProvider()
    with caplog.at_level(logging.ERROR):
        p.initialize("s1", hermes_home=str(tmp_path), platform="cli", cwd=str(tmp_path))

    assert p._enabled is False
    assert any(r.levelno >= logging.ERROR for r in caplog.records)
    assert "embedding_dim" in caplog.text
    assert "8" in caplog.text and "4" in caplog.text


def test_reopen_with_different_dim_surfaces_error(tmp_path, monkeypatch, caplog):
    # Create a DB at dim 4, then reopen requesting dim 16 (with a matching dim-16
    # embedder so the embedder<->config check passes and we hit the DB check).
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from plugins.memory.turso_vector import TursoVectorMemoryProvider

    monkeypatch.setattr(emb_mod, "make_embedder", lambda cfg: _mk(4))
    _write_dim(tmp_path, 4)
    p1 = TursoVectorMemoryProvider()
    p1.initialize("s1", hermes_home=str(tmp_path), platform="cli", cwd=str(tmp_path))
    assert p1._enabled is True
    p1.shutdown()

    monkeypatch.setattr(emb_mod, "make_embedder", lambda cfg: _mk(16))
    _write_dim(tmp_path, 16)
    p2 = TursoVectorMemoryProvider()
    with caplog.at_level(logging.ERROR):
        p2.initialize("s2", hermes_home=str(tmp_path), platform="cli", cwd=str(tmp_path))

    assert p2._enabled is False
    assert any(r.levelno >= logging.ERROR for r in caplog.records)
    txt = caplog.text.lower()
    assert "dim" in txt
    assert "4" in caplog.text and "16" in caplog.text


def test_matching_dim_enables(tmp_path, monkeypatch):
    # Sanity: when config dim matches the embedder dim, the provider enables.
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(emb_mod, "make_embedder", lambda cfg: _mk(4))
    _write_dim(tmp_path, 4)
    from plugins.memory.turso_vector import TursoVectorMemoryProvider
    p = TursoVectorMemoryProvider()
    p.initialize("s1", hermes_home=str(tmp_path), platform="cli", cwd=str(tmp_path))
    assert p._enabled is True
