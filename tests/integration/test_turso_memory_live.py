"""Live integration: TursoMemoryStore cross-device sync round-trip.

Skipped unless both TURSO_TEST_URL and TURSO_TEST_TOKEN are set, and libsql
is importable.  Marked ``integration`` (excluded from the default suite).

To run manually with credentials:

    TURSO_TEST_URL=libsql://...  TURSO_TEST_TOKEN=...  \\
        scripts/run_tests.sh --include-integration \\
            tests/integration/test_turso_memory_live.py

Exercises:
  - Write on replica A, flush to cloud.
  - Open replica B, call conn.sync(), then verify:
      * FTS recall finds the written content.
      * Native vector_search finds the written content.
"""
import os

import pytest

pytest.importorskip("libsql")

URL = os.environ.get("TURSO_TEST_URL")
TOK = os.environ.get("TURSO_TEST_TOKEN")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not (URL and TOK),
        reason="TURSO_TEST_URL/TOKEN not set",
    ),
]


def test_memory_syncs_across_devices(tmp_path):
    from agent.db_backend import SyncConfig
    from plugins.memory.turso_memory.store import TursoMemoryStore

    def sync(local):
        return SyncConfig(
            sync_url=URL, auth_token=TOK, sync_interval=5, local_path=local
        )

    marker = f"sync-probe-{os.getpid()}"

    # --- Device A: write and flush ---
    a = TursoMemoryStore(
        db_path=tmp_path / "A" / "m.db",
        dim=3,
        sync=sync(tmp_path / "A" / "m.db"),
    )
    a.add(marker, embedding=[1.0, 0.0, 0.0], embed_model="fake/3")
    a.close()  # flushes to cloud via sync on close

    # --- Device B: pull and verify ---
    b = TursoMemoryStore(
        db_path=tmp_path / "B" / "m.db",
        dim=3,
        sync=sync(tmp_path / "B" / "m.db"),
    )
    b._conn.sync()

    # FTS recall cross-device
    fts_ids = b.fts_search(marker)
    rows = b.rows_for(set(fts_ids))
    assert any(marker in r["content"] for r in rows.values()), (
        "FTS recall failed to find the written marker on replica B"
    )

    # Native vector recall cross-device (the hybrid strategy's key proof)
    vec_ids = b.vector_search([0.9, 0.1, 0.0], limit=1)
    assert vec_ids, "vector_search returned no results on replica B"
    vec_rows = b.rows_for(vec_ids)
    assert marker in vec_rows[vec_ids[0]]["content"], (
        "vector_search on replica B did not return the written marker"
    )

    b.close()
