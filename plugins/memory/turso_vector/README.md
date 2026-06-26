# turso_vector memory provider

Self-improving long-term memory backed by Turso/libSQL native vector search.
Stores corrections, insights, and user memories; recalls them semantically;
learns which memories are useful via an agent rating feedback loop and
time-decay weighting.

- Local-first: works with no Turso account (local libSQL file). With
  `database.backend: turso` set, memories sync across devices.
- Embeddings default to a local model (`all-MiniLM-L6-v2`); an API embedder is
  configurable. Enable via `hermes memory setup` → select `turso_vector`.
