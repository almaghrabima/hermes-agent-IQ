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
    # Config dim must match the fake embedder's dim (validated in initialize()).
    # I1 fix: config is now read from config["memory"]["turso_vector"].
    import yaml
    (tmp_path / "config.yaml").write_text(
        yaml.safe_dump({"memory": {"turso_vector": {"embedding_dim": 4}}}))
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
    # An old, low-weight memory should be pruned by the session-end decay sweep.
    # After the I3 fix, decay_stale() operates on ALL rows whose idle time >= 1 day,
    # so this old memory is decayed regardless of whether it was recalled this session.
    mid = provider._store.insert(kind="insight", project=None, cwd=None,
        text="stale", what_failed=None, what_worked=None,
        embedding=[1, 0, 0, 0], created_at="2026-01-01T00:00:00+00:00",
        source_session="s0", weight=0.2)
    provider._settings["decay_rate"] = 0.9
    provider._settings["weight_floor"] = 0.15
    provider.on_session_end([])
    assert provider._store.count() == 0
    assert provider._retrieved == {}


def test_recalled_memory_not_decayed_at_session_end(provider):
    # I3 contract: a memory recalled this session has last_used_at=now (set by
    # mark_used in queue_prefetch), so its idle time < 1 day at session end.
    # decay_stale() skips it — recently-used memories are exempt from decay.
    mid = provider._store.insert(kind="insight", project=None, cwd=None,
        text="recent lesson", what_failed=None, what_worked=None,
        embedding=[1, 0, 0, 0], created_at="2026-01-01T00:00:00+00:00",
        source_session="s0", weight=1.0)
    provider._settings["decay_rate"] = 0.98
    provider._settings["weight_floor"] = 0.0   # don't prune; we check the weight

    # Recall via the real queue_prefetch / prefetch path.
    provider.queue_prefetch("anything", session_id="sess-1")
    block = provider.prefetch("anything", session_id="sess-1")  # fake embedder -> [1,0,0,0] hit
    assert f"[#{mid}]" in block
    provider.on_session_end([])

    row = provider._store._conn.execute(
        "SELECT weight FROM memories WHERE id=?", (mid,)).fetchone()
    assert row is not None
    # The memory was just recalled (mark_used → last_used_at=now → idle < 1 day),
    # so decay_stale() should leave its weight unchanged.
    assert float(row[0]) == 1.0, (
        f"recalled memory weight {row[0]} should be unchanged (idle < 1 day threshold)"
    )


def test_config_schema_has_expected_keys(provider):
    keys = {f["key"] for f in provider.get_config_schema()}
    assert {"embedding_backend", "embedding_model", "top_k", "auto_extract"} <= keys


def test_shutdown_is_safe(provider):
    provider.shutdown()   # must not raise


# ---------------------------------------------------------------------------
# I3 REAL-PATH test: time-decay fires for non-recalled old rows
# ---------------------------------------------------------------------------

class _SelectiveEmbedder:
    """Deterministic embedder: fresh_memory ≈ fresh_query; old_memory is orthogonal."""
    dim = 4
    _MAP = {
        "fresh query": [1.0, 0.0, 0.0, 0.0],
        "fresh memory text": [0.99, 0.01, 0.0, 0.0],
        "old memory text": [0.0, 0.0, 0.0, 1.0],
        "warmup": [0.0, 0.0, 0.0, 0.0],
    }
    def embed(self, text):
        return self._MAP.get(text, [0.25, 0.25, 0.25, 0.25])


def test_decay_fires_for_non_recalled_old_memory(tmp_path, monkeypatch):
    """Old un-recalled memories decay at session-end; recently-used ones do not.

    This exercises the REAL queue_prefetch/mark_used/on_session_end path without
    injecting _retrieved directly (I3 real-path requirement).
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    selective = _SelectiveEmbedder()
    monkeypatch.setattr(emb_mod, "make_embedder", lambda cfg: selective)

    import yaml
    (tmp_path / "config.yaml").write_text(
        yaml.safe_dump({"memory": {"turso_vector": {"embedding_dim": 4}}}))

    from plugins.memory.turso_vector import TursoVectorMemoryProvider
    p = TursoVectorMemoryProvider()
    p.initialize("sess-decay", hermes_home=str(tmp_path), platform="cli", cwd=str(tmp_path))

    # Insert OLD memory (never recalled this session; idle for 2 days).
    # Use a 2-day-old timestamp so the memory decays but is NOT pruned
    # (0.8 * 0.98^2 ≈ 0.768 > floor=0.01, so it stays in the table).
    old_ts = "2026-06-25T00:00:00+00:00"
    old_id = p._store.insert(
        kind="insight", project=None, cwd=None,
        text="old memory text",
        what_failed=None, what_worked=None,
        embedding=selective.embed("old memory text"),
        created_at=old_ts, source_session="s0",
        weight=0.8)
    # Ensure last_used_at is also old (not NULL, which defaults to created_at).
    p._store._conn.execute(
        "UPDATE memories SET last_used_at=? WHERE id=?", (old_ts, old_id))
    p._store._conn.commit()

    # Insert FRESH memory (will be recalled by "fresh query" → mark_used → idle ≈ 0).
    fresh_id = p._store.insert(
        kind="insight", project=None, cwd=None,
        text="fresh memory text",
        what_failed=None, what_worked=None,
        embedding=selective.embed("fresh memory text"),
        created_at="2026-06-27T00:00:00+00:00", source_session="s0",
        weight=0.8)

    # Use the REAL queue_prefetch → prefetch path (not _retrieved injection).
    p.queue_prefetch("fresh query", session_id="sess-decay")
    block = p.prefetch("fresh query", session_id="sess-decay")
    assert "fresh memory text" in block, "fresh memory should have been recalled"
    # old memory should NOT appear (orthogonal embedding, high distance)
    assert "old memory text" not in block

    # Run real session-end decay.
    p._settings["decay_rate"] = 0.98
    p._settings["weight_floor"] = 0.01  # low floor so nothing is pruned yet
    p.on_session_end([])

    # OLD memory: idle ~180 days → weight decayed below initial 0.8.
    old_row = p._store._conn.execute(
        "SELECT weight FROM memories WHERE id=?", (old_id,)
    ).fetchone()
    assert old_row is not None, "old memory should still exist (weight > floor)"
    assert float(old_row[0]) < 0.8, (
        f"old memory weight {old_row[0]} should have decayed from 0.8"
    )

    # FRESH memory: last_used_at ≈ now → idle < 1 day → NOT decayed by decay_stale.
    fresh_row = p._store._conn.execute(
        "SELECT weight FROM memories WHERE id=?", (fresh_id,)
    ).fetchone()
    assert fresh_row is not None, "fresh memory should still exist"
    assert float(fresh_row[0]) == 0.8, (
        f"fresh memory weight {fresh_row[0]} should remain 0.8 (idle < 1 day)"
    )

    p.shutdown()
