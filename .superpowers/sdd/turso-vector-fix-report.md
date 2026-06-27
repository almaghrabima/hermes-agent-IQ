# turso-vector memory provider — fix report

Date: 2026-06-27  
Branch: feat/turso-vector-memory  
Worktree: .claude/worktrees/turso-vector-memory

---

## I1 — Config surface unreachable (FIXED)

**Root cause:** `_load_settings()` read `config["turso_vector"]` but `hermes memory setup` writes `config["memory"]["turso_vector"]`. Also, the whitelist filter `{k: v for k, v in cfg.items() if k in _DEFAULTS}` was redundant but present.

**Fix:** Changed the lookup to `(raw.get("memory") or {}).get("turso_vector")`. Removed the whitelist filter; instead, exclude only the one true secret (`embed_api_key`) which must remain env-only. This allows all non-secret fields (`embedding_backend`, `embedding_api_base`, `embedding_model`, `embedding_dim`, etc.) to flow through to `make_embedder`.

Updated `save_config()` to write under the same key (`config["memory"]["turso_vector"]`) so wizard round-trips work correctly.

**Real-path test:** `test_provider_config.py::test_config_routed_via_memory_key_produces_api_embedder`
- Writes config under `memory.turso_vector`
- Runs the real `_load_settings()` → `make_embedder()` path (network call NOT monkeypatched, only the `embed()` HTTP call would be if needed; but the test only calls `make_embedder`, not `embed`)
- Asserts `isinstance(embedder, APIEmbedder)` with the correct `api_base` and `model`
- RED → GREEN ✓

**Side effect:** Updated all existing test fixtures that were writing config under `{"turso_vector": {...}}` to use `{"memory": {"turso_vector": {...}}}` (files: `test_provider_recall.py`, `test_provider_session.py`, `test_provider_tools.py`, `test_integration.py`, `test_provider_config.py`, `test_provider_dim.py`).

---

## I2 — prefetch blocks the hot turn loop (FIXED)

**Root cause:** `prefetch()` called `_submit(_do, timeout=8.0)` which blocks up to 8 seconds on the caller's thread doing synchronous embedding + O(n) vector scan.

**Fix:** Added `queue_prefetch(query, *, session_id="")` that starts a background `threading.Thread` (not the executor, to avoid blocking it) which does embed + search + mark_used and caches the result under the session key. Changed `prefetch()` to join the background thread (up to 3 s safety timeout, same as sibling `turso_memory`) and return the cached string — no embedding on the caller's thread.

Added `_prefetch_lock`, `_prefetch_result`, `_prefetch_thread` to `__init__`. The `shutdown()` method joins the prefetch thread before tearing down.

**Pattern reference:** Mirrors `plugins/memory/turso_memory/__init__.py` `queue_prefetch`/`prefetch` pattern exactly.

**Real-path test:** `test_provider_recall.py::test_prefetch_does_not_embed_on_main_thread`
- `_ThreadRecordingEmbedder` records the thread ID of every `embed()` call
- Calls `queue_prefetch()` then `prefetch()` on the main thread
- Asserts every recorded thread ID ≠ `threading.get_ident()` (main thread)
- RED → GREEN ✓

**Updated tests:** All tests that called `provider.prefetch(...)` directly (without a prior `queue_prefetch`) were updated to call `queue_prefetch` first: `test_recall_returns_semantically_nearest`, `test_recall_block_surfaces_memory_id`, `test_full_loop_recall_then_rate`, `test_recalled_memory_not_decayed_at_session_end`.

---

## I3 — Time-decay never fires (FIXED)

**Root cause:** `_decay_sweep` only operated on `ids = list(self._retrieved.keys())`. Since `queue_prefetch` had just called `mark_used` on those same IDs (setting `last_used_at=now`), their idle time was ~0 → `decay_weight(w, 0, rate) = w` → no decay. Non-recalled rows were also never swept.

**Fix:** Added `VectorStore.decay_stale(*, now, decay_rate, weight_floor, min_idle_days=1.0)` to `store.py` which:
1. Fetches ALL rows
2. Computes `days = _days_between(COALESCE(last_used_at, created_at), now)`
3. Only decays rows where `days >= min_idle_days` (so recently-recalled rows with idle≈0 are exempt)
4. Runs the same table-wide GC (prune all sub-floor rows)

Changed `_decay_sweep` to call `store.decay_stale(...)` instead of `store.decay_and_prune(ids=[...])`. Removed the `not self._retrieved` early-return guard so the sweep fires even if nothing was recalled this session.

The `prior_used` trick is no longer needed: recalled memories get `last_used_at=now` → idle < 1 day → skipped by `decay_stale`. Non-recalled old memories have their genuine idle time and are correctly decayed.

**Real-path test:** `test_provider_session.py::test_decay_fires_for_non_recalled_old_memory`
- Inserts an OLD memory (last_used_at = "2026-06-25", idle=2 days) with orthogonal embedding to the query
- Inserts a FRESH memory (created "2026-06-27", recalled by `queue_prefetch` → `prefetch`)
- Runs real `on_session_end` decay
- Asserts OLD memory weight < 0.8 (decayed by `0.98^2 ≈ 0.96`)
- Asserts FRESH memory weight == 0.8 (exempt: idle < 1 day)
- Does NOT inject `_retrieved` directly
- RED → GREEN ✓

**Updated test:** `test_prefetch_then_session_end_decays_by_idle_time` was renamed to `test_recalled_memory_not_decayed_at_session_end` and its assertion inverted: with the new design, recently-recalled memories are exempt from decay (weight stays unchanged), which is the correct new contract.

---

## Minor — override save_config (FIXED)

Already implemented as part of I1 fix: `save_config(values, hermes_home)` now writes under `config["memory"]["turso_vector"]` (matching what `_load_settings` reads). Does not write `embed_api_key` (env-only). Existing YAML content is preserved.

---

## Minor — don't report failure as success (FIXED)

`_dispatch` for `memory_report` and `memory_remember` now returns `{"status": "error", "error": "store op failed or timed out"}` when `_store_memory` returns `None`, instead of `{"stored_id": null}`.

---

## Test results

```
=== Summary: 11 files, 44 tests passed, 0 failed (100% complete) ===
```

`ruff check plugins/memory/turso_vector/` → All checks passed.

---

## Files changed

- `plugins/memory/turso_vector/__init__.py` — I1, I2, I3, both minors
- `plugins/memory/turso_vector/store.py` — added `decay_stale()`
- `tests/turso_vector_plugin/test_provider_recall.py` — config key fix, I2 real-path test
- `tests/turso_vector_plugin/test_provider_session.py` — config key fix, I3 real-path test
- `tests/turso_vector_plugin/test_provider_tools.py` — config key fix
- `tests/turso_vector_plugin/test_integration.py` — config key fix, queue_prefetch calls
- `tests/turso_vector_plugin/test_provider_config.py` — config key fix, I1 real-path test
- `tests/turso_vector_plugin/test_provider_dim.py` — config key fix
