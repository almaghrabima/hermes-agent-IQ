# Turso / libSQL backend — cross-device sync

Hermes can run its SQLite stores as **libSQL embedded replicas** that sync to a
Turso cloud database in the background. Reads and writes stay local-fast (a real
file on disk); a background sync pushes/pulls changes so the same sessions and
kanban boards are available on every device you run Hermes from.

This backend is **opt-in and off by default** — with no configuration, Hermes
uses a plain local `sqlite3` database exactly as before.

## What syncs

| Store | Local file | Syncs under Turso? |
|-------|-----------|--------------------|
| Sessions / messages (`state.db`) | `<hermes_home>/replicas/state.db` | yes |
| Kanban boards (`kanban.db`) | `<hermes_home>/replicas/kanban/…` | yes |
| Turso memory plugin (optional) | its own replica | yes (configured separately — see below) |

The inherently-local stores (Temporal outbox, retain queue, backups, diagnostics)
stay local on purpose — they have no cross-device meaning.

## Multi-device safety

Concurrent writes from more than one device are **collision-free**. Every synced
row that used to rely on `AUTOINCREMENT` now gets a device-partitioned 63-bit
Snowflake id, so two devices (e.g. an always-on gateway plus your laptop) can
append messages or kanban activity at the same time without overwriting each
other. This was verified against a live Turso database
(`docs/superpowers/specs/2026-06-27-libsql-sync-semantics-findings.md`): distinct-id
appends from two replicas both survive a sync cycle.

**Residual to know about:** in-place updates to the *same row* from two devices at
the same time (e.g. a session's `message_count`, a task's status) resolve
**last-write-wins**. The message/comment/event **rows** themselves — the actual
data-loss risk — never collide. For the common "one interactive device + a
background gateway" pattern this is safe.

## Enable it

### 1. Create a Turso database and get its URL + token

Using the [Turso CLI](https://docs.turso.tech/cli):

```bash
turso db create hermes              # create the database
turso db show hermes --url          # -> libsql://hermes-<org>.<region>.turso.io
turso db tokens create hermes       # -> a database auth token (rw, ~1y default)
```

> The token must be a **database** auth token (its JWT payload includes `"a":"rw"`),
> not an org/platform API key — an org key is rejected at sync time with
> `401 Unauthorized: invalid JWT token`.

### 2. Put the secret in `~/.hermes/.env`

Secrets only — never in `config.yaml`:

```dotenv
TURSO_AUTH_TOKEN=eyJhbGci...        # the db token from step 1
```

### 3. Turn the backend on in `~/.hermes/config.yaml`

```yaml
database:
  backend: turso                    # default is "sqlite" (local-only)
  turso:
    sync_url: "libsql://hermes-<org>.<region>.turso.io"   # from step 1
    sync_interval: 60               # seconds between background syncs (default 60)
    # local_path: optional override; defaults to <hermes_home>/replicas/state.db
```

Paths are profile-aware — under a non-default profile the replica lives in that
profile's home, so multiple profiles don't share one cloud DB by accident.

### 4. Verify

```bash
hermes doctor
```

The **Database Backend** check reports the resolved backend and, under Turso,
confirms sync is collision-free. If `backend: turso` is set but `sync_url` or
`TURSO_AUTH_TOKEN` is missing, startup fails **loudly** with `BackendConfigError`
rather than silently falling back to a local DB (which would split-brain your data).

## Notes

- **Same token, repeat it per consumer.** The `turso_memory` plugin (if you use it)
  reads its own `plugins.turso_memory.sync_url` and the same `TURSO_AUTH_TOKEN`. The
  session/kanban backend and the memory plugin are configured independently — enabling
  one does not enable the other.
- **Engine.** Hermes uses the stable synchronous `libsql` client; the connection layer
  (`agent/db_backend.py`) is engine-swappable. `conn.sync()` is *replica
  synchronization*; the connection API itself stays synchronous.
- **Turning it off.** Set `database.backend: sqlite` (or remove the `database` block).
  Hermes returns to a plain local `sqlite3` database — byte-identical to never having
  enabled it. Your local replica file remains on disk.
- **First run after enabling** pulls the current cloud state into the local replica; if
  the cloud DB is empty it starts fresh and your existing local data is not auto-migrated
  into it. Plan the cutover (e.g. start clean, or seed the cloud DB) deliberately.
