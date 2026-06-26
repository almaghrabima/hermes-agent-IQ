# Temporal Phase 4b — Durable Kanban Spawn Backend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a kanban card's worker crash-durable by adding an opt-in Temporal spawn backend that runs the card's `hermes chat` subprocess inside a supervised Temporal activity, surviving a host/gateway crash.

**Architecture:** Kanban's dispatch tick already takes a `spawn_fn` seam. We extract the spawn's argv/env/log construction into a pure, serializable `build_spawn_args`, then add a temporal `spawn_fn` (selected by `kanban.spawn_provider: temporal`) that starts a `KanbanTaskWorkflow` per claimed card. The workflow's single activity Popens the same subprocess and heartbeats it; Temporal becomes the sole supervisor for its runs, so SQLite's TTL/heartbeat reclaimers skip `run_kind='temporal'` rows to prevent double-execution. Built-in subprocess spawn stays the default; everything is inert unless opted in.

**Tech Stack:** Python 3.11, `temporalio==1.29.0` (optional `[temporal]` extra, lazy-imported), SQLite (kanban store), pytest via `scripts/run_tests.sh`.

## Global Constraints

- **Opt-in, zero regression when unselected:** built-in `_default_spawn` stays the default; the temporal path is reached only via `kanban.spawn_provider: temporal`. (spec Invariant 1)
- **Never leave kanban without a spawn:** the resolver falls back to `_default_spawn` if temporal is disabled/unavailable; an individual spawn that can't reach Temporal falls back to `_default_spawn` for that tick. No card is ever dropped. (spec Invariant 2)
- **At-most-once per card run:** exactly one supervisor is authoritative for a Temporal-backed run. (spec Invariant 3)
- **Tests use a temp `HERMES_HOME`, never the real `~/.hermes/`.** Run via `scripts/run_tests.sh` (CI parity: unsets creds, `TZ=UTC`, subprocess isolation).
- **`temporalio` is lazy-imported** behind `try/except ImportError`; gated e2e tests `pytest.importorskip("temporalio")`.
- **No new `HERMES_*` env vars for non-secret config** — the new setting is `kanban.spawn_provider` in `config.yaml`.
- **Paths are profile-aware** — never hardcode `~/.hermes`; the spawn args already resolve board-scoped paths via existing kanban helpers.
- **Commit messages** end with: `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`
- **Documented limitation:** the activity Popens on the worker host, so the worker must reach the card's `workspace_path` (same host or shared FS). Cross-host workspaces are out of scope.

---

## File Structure

| File | Responsibility |
|---|---|
| `hermes_cli/kanban_db.py` (modify) | Extract `build_spawn_args` (pure, serializable) + `_popen_from_spawn_args` (Popen + log) out of `_default_spawn`; add `run_kind` column + `_mark_run_temporal`/`_clear_run_kind`; skip `run_kind='temporal'` in `release_stale_claims` and `detect_stale_running`; add `reap_temporal_worker`; wire `resolve_kanban_spawn` into `_dispatch_once_locked` (both call sites). |
| `hermes_cli/kanban_spawn_provider.py` (create) | `resolve_kanban_spawn(cfg) -> spawn_fn` — reads `kanban.spawn_provider`, returns `_default_spawn` (default/fallback) or the temporal spawn (tagged `_kanban_run_kind='temporal'`). |
| `hermes_cli/config.py` (modify) | Add `kanban.spawn_provider` default `"builtin"`. |
| `plugins/kanban_spawn_temporal/__init__.py` (create) | The temporal `spawn_fn(task, workspace, board)` — `build_spawn_args` → `start_workflow("KanbanTaskWorkflow", …)`; returns falsy pid; per-tick fallback to `_default_spawn` on connect/start error. |
| `plugins/temporal/activities.py` (modify) | `run_kanban_worker(payload)` activity — Popen via `_popen_from_spawn_args`, heartbeat loop, `reap_temporal_worker` on exit. |
| `plugins/temporal/workflows.py` (modify) | `KanbanTaskWorkflow` + `_make_kanban_task_workflow()` — one activity call, retry `1 + failure_limit`, `start_to_close` from `max_runtime_seconds`. |
| `plugins/temporal/worker.py` (modify) | Register `KanbanTaskWorkflow` + `run_kanban_worker`. |
| `AGENTS.md` (modify) | Document `kanban.spawn_provider: temporal` under the Temporal section. |

**Tests:**
- `tests/kanban/test_kanban_spawn_args.py` (create) — `build_spawn_args` parity + `_popen_from_spawn_args`.
- `tests/kanban/test_kanban_run_kind_reclaim.py` (create) — reclaim-skip for temporal runs.
- `tests/kanban/test_kanban_spawn_provider.py` (create) — resolver selection + fallback.
- `tests/kanban/test_kanban_reap_temporal.py` (create) — `reap_temporal_worker` outcomes.
- `tests/temporal/test_kanban_workflow.py` (create) — workflow/activity gated e2e + retry config.

> **Note on file size:** `hermes_cli/kanban_db.py` is already very large. Per repo convention we do **not** restructure it; new helpers are added alongside the functions they relate to. The provider resolver lives in its own small file (`kanban_spawn_provider.py`) to keep the seam greppable.

---

## Task 1: Extract `build_spawn_args` + `_popen_from_spawn_args`

Pure refactor of `_default_spawn` (`hermes_cli/kanban_db.py:7286-7456`). Splits it into a serializable args-builder and a Popen executor, with `_default_spawn` recomposed from both. Behavior must be byte-identical for the builtin path.

**Files:**
- Modify: `hermes_cli/kanban_db.py:7286-7456`
- Test: `tests/kanban/test_kanban_spawn_args.py`

**Interfaces:**
- Produces:
  - `build_spawn_args(task: Task, workspace: str, *, board: Optional[str] = None) -> dict` returning JSON-serializable keys: `{"argv": list[str], "cwd": Optional[str], "env_overlay": dict[str, str], "log_path": str, "max_runtime_seconds": Optional[int]}`. `env_overlay` contains ONLY the keys `_default_spawn` sets (NOT the full `os.environ`).
  - `_popen_from_spawn_args(args: dict) -> "subprocess.Popen"` — opens/rotates the log at `args["log_path"]`, builds `env = {**os.environ, **args["env_overlay"]}`, and Popens `args["argv"]` with `cwd=args["cwd"]`.
  - `_default_spawn` unchanged signature/return (`-> Optional[int]`), now `return _popen_from_spawn_args(build_spawn_args(task, workspace, board=board)).pid`.

- [ ] **Step 1: Write the failing test**

```python
# tests/kanban/test_kanban_spawn_args.py
import os
from hermes_cli import kanban_db


def _mk_task(**kw):
    t = kanban_db.Task(id="t-1", title="x", status="running")
    t.assignee = kw.get("assignee", "default")
    t.workspace_kind = kw.get("workspace_kind", "shared")
    t.current_run_id = kw.get("current_run_id", 7)
    t.claim_lock = kw.get("claim_lock", "host:abc")
    t.max_runtime_seconds = kw.get("max_runtime_seconds", 1800)
    return t


def test_build_spawn_args_overlay_excludes_full_environ(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("SOME_UNRELATED_VAR", "leak-me")
    task = _mk_task()
    args = kanban_db.build_spawn_args(task, str(tmp_path), board=None)
    # Overlay carries kanban-specific keys but NOT arbitrary host env.
    assert args["env_overlay"]["HERMES_KANBAN_TASK"] == "t-1"
    assert "SOME_UNRELATED_VAR" not in args["env_overlay"]
    # argv invokes `chat -q "work kanban task t-1"`.
    assert args["argv"][-2:] == ["-q", "work kanban task t-1"]
    assert args["max_runtime_seconds"] == 1800
    # JSON-serializable.
    import json
    json.dumps(args)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `scripts/run_tests.sh tests/kanban/test_kanban_spawn_args.py::test_build_spawn_args_overlay_excludes_full_environ`
Expected: FAIL with `AttributeError: module 'hermes_cli.kanban_db' has no attribute 'build_spawn_args'`.

- [ ] **Step 3: Implement `build_spawn_args` + `_popen_from_spawn_args`**

Replace the body of `_default_spawn` (`hermes_cli/kanban_db.py:7286-7456`). Move everything that computes `env` and `cmd` into `build_spawn_args`, but **start `env_overlay` as an empty dict** (not `dict(os.environ)`) and only assign the keys `_default_spawn` currently sets. Move the log + Popen block into `_popen_from_spawn_args`.

```python
def build_spawn_args(
    task: Task,
    workspace: str,
    *,
    board: Optional[str] = None,
) -> dict:
    """Pure, JSON-serializable spawn description shared by the builtin
    subprocess spawn and the Temporal activity. Computes the argv, the
    kanban-specific env OVERLAY (NOT the full os.environ), the cwd, the
    per-task log path, and the runtime cap. All board-scoped paths are
    resolved here (dispatcher-side); the executor re-overlays the overlay
    on its own host os.environ.
    """
    if not task.assignee:
        raise ValueError(f"task {task.id} has no assignee")
    from hermes_cli.profiles import normalize_profile_name, resolve_profile_env

    profile_arg = normalize_profile_name(task.assignee)
    overlay: dict[str, str] = {}
    try:
        overlay["HERMES_HOME"] = resolve_profile_env(profile_arg)
    except FileNotFoundError:
        pass
    if task.tenant:
        overlay["HERMES_TENANT"] = task.tenant
    overlay["HERMES_KANBAN_TASK"] = task.id
    overlay["HERMES_KANBAN_WORKSPACE"] = workspace
    if workspace and os.path.isabs(workspace) and os.path.isdir(workspace):
        overlay["TERMINAL_CWD"] = workspace
    if task.branch_name:
        overlay["HERMES_KANBAN_BRANCH"] = task.branch_name
    if task.current_run_id is not None:
        overlay["HERMES_KANBAN_RUN_ID"] = str(task.current_run_id)
    if task.claim_lock:
        overlay["HERMES_KANBAN_CLAIM_LOCK"] = task.claim_lock
    if task.goal_mode:
        overlay["HERMES_KANBAN_GOAL_MODE"] = "1"
        if task.goal_max_turns is not None:
            overlay["HERMES_KANBAN_GOAL_MAX_TURNS"] = str(int(task.goal_max_turns))
    terminal_timeout = _worker_terminal_timeout_env(
        task.max_runtime_seconds, os.environ.get("TERMINAL_TIMEOUT"),
    )
    if terminal_timeout is not None:
        overlay["TERMINAL_TIMEOUT"] = terminal_timeout
    foreground_timeout = _worker_terminal_timeout_env(
        task.max_runtime_seconds, os.environ.get("TERMINAL_MAX_FOREGROUND_TIMEOUT"),
    )
    if foreground_timeout is not None:
        overlay["TERMINAL_MAX_FOREGROUND_TIMEOUT"] = foreground_timeout
    overlay["HERMES_KANBAN_DB"] = str(kanban_db_path(board=board))
    overlay["HERMES_KANBAN_WORKSPACES_ROOT"] = str(workspaces_root(board=board))
    overlay["HERMES_KANBAN_BOARD"] = _normalize_board_slug(board) or get_current_board()
    overlay["HERMES_PROFILE"] = profile_arg

    cmd = [*_resolve_hermes_argv(), "-p", profile_arg, "--accept-hooks"]
    if task.skills:
        for sk in task.skills:
            if sk:
                cmd.extend(["--skills", sk])
    if task.model_override:
        cmd.extend(["-m", task.model_override])
    worker_toolsets = _resolve_worker_cli_toolsets(overlay.get("HERMES_HOME"))
    if worker_toolsets:
        cmd.extend(["--toolsets", ",".join(worker_toolsets)])
    cmd.extend(["chat", "-q", f"work kanban task {task.id}"])

    log_dir = worker_logs_dir(board=board)
    log_path = log_dir / f"{task.id}.log"
    return {
        "argv": cmd,
        "cwd": workspace if os.path.isdir(workspace) else None,
        "env_overlay": overlay,
        "log_path": str(log_path),
        "max_runtime_seconds": (
            int(task.max_runtime_seconds)
            if task.max_runtime_seconds is not None else None
        ),
    }


def _popen_from_spawn_args(args: dict) -> "subprocess.Popen":
    """Execute a ``build_spawn_args`` description: rotate+open the log,
    overlay the env on the host os.environ, and Popen. Returns the live
    Popen handle (caller reads ``.pid`` for fire-and-forget, or polls it
    for supervision). Intentionally does NOT close the log file handle —
    the child keeps writing after this returns.
    """
    import subprocess

    env = {**os.environ, **args["env_overlay"]}
    log_path = Path(args["log_path"])
    log_path.parent.mkdir(parents=True, exist_ok=True)
    rotate_bytes, backup_count = worker_log_rotation_config()
    _rotate_worker_log(log_path, rotate_bytes, backup_count)
    log_f = open(log_path, "ab")
    try:
        return subprocess.Popen(  # noqa: S603 -- argv is a fixed list built above
            args["argv"],
            cwd=args["cwd"],
            stdin=subprocess.DEVNULL,
            stdout=log_f,
            stderr=subprocess.STDOUT,
            env=env,
            start_new_session=True,
            creationflags=subprocess.CREATE_NO_WINDOW if _IS_WINDOWS else 0,
        )
    except FileNotFoundError:
        log_f.close()
        raise RuntimeError(
            "`hermes` executable not found on PATH. "
            "Install Hermes Agent or activate its venv before running the kanban dispatcher."
        )


def _default_spawn(
    task: Task,
    workspace: str,
    *,
    board: Optional[str] = None,
) -> Optional[int]:
    """Fire-and-forget ``hermes -p <profile> chat -q ...`` subprocess.
    Returns the spawned child's PID so the dispatcher can detect crashes.
    """
    return _popen_from_spawn_args(build_spawn_args(task, workspace, board=board)).pid
```

> If `Path` is not already imported at module top, it is (`pathlib.Path` is used throughout kanban_db). Verify `_IS_WINDOWS`, `worker_log_rotation_config`, `_rotate_worker_log`, `worker_logs_dir`, `kanban_db_path`, `workspaces_root`, `_normalize_board_slug`, `get_current_board`, `_resolve_hermes_argv`, `_resolve_worker_cli_toolsets`, `_worker_terminal_timeout_env` are all module-level (they are — all were referenced inside the original `_default_spawn`).

- [ ] **Step 4: Add a parity test and run all**

```python
# tests/kanban/test_kanban_spawn_args.py  (append)
def test_default_spawn_uses_build_then_popen(tmp_path, monkeypatch):
    captured = {}

    def fake_popen(args):
        class P:  # minimal stand-in
            pid = 4321
        captured["args"] = args
        return P()

    monkeypatch.setattr(kanban_db, "_popen_from_spawn_args", fake_popen)
    task = _mk_task()
    pid = kanban_db._default_spawn(task, str(tmp_path), board=None)
    assert pid == 4321
    assert captured["args"]["env_overlay"]["HERMES_KANBAN_TASK"] == "t-1"
```

Run: `scripts/run_tests.sh tests/kanban/test_kanban_spawn_args.py`
Expected: PASS (both tests).

- [ ] **Step 5: Run the existing kanban spawn/dispatch tests to prove no regression**

Run: `scripts/run_tests.sh tests/kanban/`
Expected: PASS — the builtin spawn path is unchanged in behavior.

- [ ] **Step 6: Commit**

```bash
git add hermes_cli/kanban_db.py tests/kanban/test_kanban_spawn_args.py
git commit -m "refactor(kanban): extract build_spawn_args + _popen_from_spawn_args from _default_spawn

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: `run_kind` column + reclaim-skip for Temporal runs

Adds a `run_kind` marker so SQLite's TTL/heartbeat reclaimers leave Temporal-supervised runs alone (preventing double-execution on a worker-host crash). PID-gated reclaimers (`detect_crashed_workers`, max-runtime enforcer) already auto-skip because Temporal runs keep `worker_pid` NULL.

**Files:**
- Modify: `hermes_cli/kanban_db.py` — schema (`:1033`, `:1817`), `release_stale_claims:3389`, `detect_stale_running:5830`; add `_mark_run_temporal` / `_clear_run_kind`.
- Test: `tests/kanban/test_kanban_run_kind_reclaim.py`

**Interfaces:**
- Produces:
  - new `tasks.run_kind TEXT` column (NULL = builtin subprocess; `"temporal"` = Temporal-supervised).
  - `_mark_run_temporal(conn: sqlite3.Connection, task_id: str) -> None` — sets `run_kind='temporal'`, leaves `worker_pid` NULL, emits a `spawned` event with `{"run_kind": "temporal"}`.
  - `_clear_run_kind(conn: sqlite3.Connection, task_id: str) -> None` — resets `run_kind=NULL` (called by the builtin pid setter so a builtin re-spawn after a temporal one is accurate).
- Consumes: `_append_event`, `_current_run_id`, `write_txn`, `DEFAULT_CLAIM_HEARTBEAT_MAX_STALE_SECONDS` (existing).

- [ ] **Step 1: Write the failing test**

```python
# tests/kanban/test_kanban_run_kind_reclaim.py
import time
from hermes_cli import kanban_db


def _running_task(conn, *, run_kind=None, claim_age=10_000):
    # Insert a 'running' task with an expired claim and no live pid.
    now = int(time.time())
    tid = "t-rk"
    kanban_db.add_task(conn, id=tid, title="x", assignee="default")
    with kanban_db.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET status='running', claim_lock=?, "
            "claim_expires=?, worker_pid=NULL, run_kind=? WHERE id=?",
            (f"{kanban_db._claimer_id().split(':',1)[0]}:lock", now - claim_age, run_kind, tid),
        )
    return tid


def test_release_stale_claims_skips_temporal_runs(kanban_conn):
    tid = _running_task(kanban_conn, run_kind="temporal")
    reclaimed = kanban_db.release_stale_claims(kanban_conn)
    row = kanban_conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()
    assert reclaimed == 0
    assert row["status"] == "running"   # NOT reclaimed


def test_release_stale_claims_still_reclaims_builtin(kanban_conn):
    tid = _running_task(kanban_conn, run_kind=None)
    kanban_db.release_stale_claims(kanban_conn)
    row = kanban_conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()
    assert row["status"] == "ready"     # builtin run with dead pid → reclaimed
```

> Use the suite's existing kanban DB fixture if one exists (search `tests/kanban/conftest.py` for a `kanban_conn`/temp-DB fixture and reuse it; if none, create a temp-`HERMES_HOME` connection via `kanban_db.connect()` against `tmp_path`). `add_task`/`_claimer_id`/`write_txn` are existing module symbols.

- [ ] **Step 2: Run test to verify it fails**

Run: `scripts/run_tests.sh tests/kanban/test_kanban_run_kind_reclaim.py`
Expected: FAIL — either `sqlite3.OperationalError: no such column: run_kind`, or `test_release_stale_claims_skips_temporal_runs` reclaims the task.

- [ ] **Step 3: Add the column (schema + migration)**

In the `CREATE TABLE tasks` DDL (`hermes_cli/kanban_db.py:~1033`, the block with `worker_pid INTEGER,`), add a column:

```python
    worker_pid           INTEGER,
    run_kind             TEXT,
```

In the migration block (`hermes_cli/kanban_db.py:~1817`, next to the `worker_pid` migration):

```python
    if "run_kind" not in cols:
        _add_column_if_missing(conn, "tasks", "run_kind", "run_kind TEXT")
```

- [ ] **Step 4: Add the marker helpers**

Add next to `_set_worker_pid` (`hermes_cli/kanban_db.py:~6339`):

```python
def _mark_run_temporal(conn: sqlite3.Connection, task_id: str) -> None:
    """Mark the current run Temporal-supervised. Leaves worker_pid NULL so
    the PID-gated reclaimers (detect_crashed_workers, enforce_max_runtime)
    skip it, and sets run_kind='temporal' so the TTL/heartbeat reclaimers
    (release_stale_claims, detect_stale_running) skip it too. Temporal is
    then the sole supervisor for this run."""
    with write_txn(conn):
        conn.execute("UPDATE tasks SET run_kind='temporal' WHERE id = ?", (task_id,))
        run_id = _current_run_id(conn, task_id)
        _append_event(conn, task_id, "spawned", {"run_kind": "temporal"}, run_id=run_id)


def _clear_run_kind(conn: sqlite3.Connection, task_id: str) -> None:
    """Reset run_kind to NULL (builtin). Called by the builtin pid setter so
    a builtin re-spawn after a temporal attempt is recorded accurately."""
    with write_txn(conn):
        conn.execute("UPDATE tasks SET run_kind=NULL WHERE id = ?", (task_id,))
```

Append `_clear_run_kind(conn, task_id)` to the end of `_set_worker_pid`'s `write_txn` body (so builtin spawns always have `run_kind=NULL`):

```python
        _append_event(conn, task_id, "spawned", {"pid": int(pid)}, run_id=run_id)
        conn.execute("UPDATE tasks SET run_kind=NULL WHERE id = ?", (task_id,))
```

- [ ] **Step 5: Add the skip to the two TTL/heartbeat reclaimers**

In `release_stale_claims` (`:3422`), add `run_kind` to the SELECT and skip temporal rows at the top of the loop:

```python
    stale = conn.execute(
        "SELECT id, claim_lock, worker_pid, claim_expires, last_heartbeat_at, run_kind "
        "FROM tasks "
        "WHERE status = 'running' AND claim_expires IS NOT NULL "
        "  AND claim_expires < ?",
        (now,),
    ).fetchall()
    for row in stale:
        if (row["run_kind"] if "run_kind" in row.keys() else None) == "temporal":
            continue  # Temporal supervises its own runs; never reclaim here.
```

In `detect_stale_running` (`:5866`), add `run_kind` to the SELECT and skip:

```python
    rows = conn.execute(
        "SELECT t.id, t.worker_pid, t.last_heartbeat_at, t.claim_lock, t.run_kind, "
        "       COALESCE(r.started_at, t.started_at) AS active_started_at "
        "FROM tasks t "
        "LEFT JOIN task_runs r ON r.id = t.current_run_id "
        "WHERE t.status = 'running'"
    ).fetchall()

    for row in rows:
        if (row["run_kind"] if "run_kind" in row.keys() else None) == "temporal":
            continue  # Temporal supervises its own runs; never reclaim here.
```

- [ ] **Step 6: Run the tests**

Run: `scripts/run_tests.sh tests/kanban/test_kanban_run_kind_reclaim.py`
Expected: PASS (both).

- [ ] **Step 7: Run the full kanban suite for regressions**

Run: `scripts/run_tests.sh tests/kanban/`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add hermes_cli/kanban_db.py tests/kanban/test_kanban_run_kind_reclaim.py
git commit -m "feat(kanban): run_kind marker so TTL/heartbeat reclaimers skip Temporal-supervised runs

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: `reap_temporal_worker` — record outcome of a Temporal-run subprocess

Because the SQLite reapers skip Temporal runs, the activity must reap its own subprocess: translate the subprocess exit code into the same SQLite outcome the builtin reapers would (terminal-already / protocol-violation / rate-limit / failure). This is a focused parallel of `detect_crashed_workers`' post-death logic for the single-process case.

**Files:**
- Modify: `hermes_cli/kanban_db.py` — add `reap_temporal_worker`.
- Test: `tests/kanban/test_kanban_reap_temporal.py`

**Interfaces:**
- Produces: `reap_temporal_worker(conn: sqlite3.Connection, task_id: str, exit_code: int, *, board: Optional[str] = None) -> str` returning one of `"terminal"` (card already done/blocked — worker called the tool), `"protocol_violation"` (exit 0 but still running — breaker tripped), `"rate_limited"` (exit == `KANBAN_RATE_LIMIT_EXIT_CODE` — released without counting a failure), `"failed"` (non-zero — failure counted).
- Consumes: `get_task`, `_record_task_failure`, `KANBAN_RATE_LIMIT_EXIT_CODE` (existing — search to confirm the constant name; it is referenced in `detect_crashed_workers`'s docstring), `write_txn`, `_append_event`, `_resolve_failure_limit` or the passed-in limit.

- [ ] **Step 1: Write the failing test**

```python
# tests/kanban/test_kanban_reap_temporal.py
from hermes_cli import kanban_db


def test_reap_terminal_when_card_already_done(kanban_conn):
    kanban_db.add_task(kanban_conn, id="t-done", title="x", assignee="default")
    # Simulate the worker having completed the card itself.
    with kanban_db.write_txn(kanban_conn):
        kanban_conn.execute("UPDATE tasks SET status='done' WHERE id='t-done'")
    assert kanban_db.reap_temporal_worker(kanban_conn, "t-done", 0) == "terminal"


def test_reap_protocol_violation_on_clean_exit_still_running(kanban_conn):
    kanban_db.add_task(kanban_conn, id="t-pv", title="x", assignee="default")
    with kanban_db.write_txn(kanban_conn):
        kanban_conn.execute(
            "UPDATE tasks SET status='running', run_kind='temporal' WHERE id='t-pv'")
    assert kanban_db.reap_temporal_worker(kanban_conn, "t-pv", 0) == "protocol_violation"


def test_reap_failed_on_nonzero_exit(kanban_conn):
    kanban_db.add_task(kanban_conn, id="t-f", title="x", assignee="default")
    with kanban_db.write_txn(kanban_conn):
        kanban_conn.execute(
            "UPDATE tasks SET status='running', run_kind='temporal' WHERE id='t-f'")
    assert kanban_db.reap_temporal_worker(kanban_conn, "t-f", 1) == "failed"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `scripts/run_tests.sh tests/kanban/test_kanban_reap_temporal.py`
Expected: FAIL with `AttributeError: ... has no attribute 'reap_temporal_worker'`.

- [ ] **Step 3: Implement `reap_temporal_worker`**

Add near `detect_crashed_workers` (`hermes_cli/kanban_db.py:~5970`). First confirm the rate-limit constant name with `grep -n "RATE_LIMIT_EXIT_CODE" hermes_cli/kanban_db.py`; substitute the real name below.

```python
def reap_temporal_worker(
    conn: sqlite3.Connection,
    task_id: str,
    exit_code: int,
    *,
    board: Optional[str] = None,
) -> str:
    """Record the outcome of a Temporal-supervised worker subprocess after
    it exits. Mirrors detect_crashed_workers' post-death branches for the
    single-process case, since the SQLite reapers skip run_kind='temporal'.
    """
    task = get_task(conn, task_id)
    if task is None:
        return "terminal"
    # Worker already drove the card to a terminal state via kanban_complete/
    # kanban_block — nothing to reap.
    if task.status in ("done", "blocked", "archived", "review"):
        return "terminal"
    # Provider quota wall — release WITHOUT counting a failure.
    if exit_code == KANBAN_RATE_LIMIT_EXIT_CODE:
        with write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status='ready', claim_lock=NULL, "
                "claim_expires=NULL, worker_pid=NULL, run_kind=NULL "
                "WHERE id=? AND status='running'",
                (task_id,),
            )
            _append_event(conn, task_id, "rate_limited", {"exit_code": exit_code})
        return "rate_limited"
    if exit_code == 0:
        # Clean exit but still running → worker never called complete/block.
        # Trip the breaker immediately (protocol violation), like the
        # builtin reaper does for rc=0-but-running.
        _record_task_failure(
            conn, task_id,
            error="temporal worker exited 0 without a terminal kanban transition",
            outcome="crashed", release_claim=True, end_run=True,
            event_payload_extra={"protocol_violation": True, "run_kind": "temporal"},
        )
        return "protocol_violation"
    # Non-zero exit → count a failure; the breaker may trip to blocked.
    _record_task_failure(
        conn, task_id,
        error=f"temporal worker exited {exit_code}",
        outcome="crashed", release_claim=True, end_run=True,
        event_payload_extra={"exit_code": exit_code, "run_kind": "temporal"},
    )
    return "failed"
```

> Match `_record_task_failure`'s real keyword arguments — confirm with `grep -n "def _record_task_failure" hermes_cli/kanban_db.py` and read its signature. The call above uses `release_claim`/`end_run`/`event_payload_extra`, which appear at its other call sites (`:5812`, `:6934`). Adjust `outcome=` to a value the function accepts (`"crashed"` is used by the crash reaper).

- [ ] **Step 4: Run the tests**

Run: `scripts/run_tests.sh tests/kanban/test_kanban_reap_temporal.py`
Expected: PASS (all three).

- [ ] **Step 5: Commit**

```bash
git add hermes_cli/kanban_db.py tests/kanban/test_kanban_reap_temporal.py
git commit -m "feat(kanban): reap_temporal_worker — record subprocess outcome for Temporal runs

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: `kanban.spawn_provider` config + `resolve_kanban_spawn`

The selection seam: a tiny resolver that returns either the builtin spawn or the temporal spawn, with config-only availability (no network) and fallback. Mirrors cron's `resolve_cron_scheduler` contract.

**Files:**
- Create: `hermes_cli/kanban_spawn_provider.py`
- Modify: `hermes_cli/config.py:2390` (add default)
- Test: `tests/kanban/test_kanban_spawn_provider.py`

**Interfaces:**
- Consumes: `kanban_db._default_spawn` (Task 1); `plugins.temporal.tconfig.resolve_temporal_config` + `load_config` (existing, to check `temporal.enabled`); the temporal `spawn_fn` from `plugins.kanban_spawn_temporal` (Task 7) — imported lazily inside the function so this task's tests pass before Task 7 exists (the import failure path is exercised directly).
- Produces: `resolve_kanban_spawn(cfg: dict | None = None) -> Callable`. The returned callable always has signature `(task, workspace, *, board=None) -> Optional[int]`. The temporal callable is tagged `func._kanban_run_kind = "temporal"`.

- [ ] **Step 1: Write the failing test**

```python
# tests/kanban/test_kanban_spawn_provider.py
from hermes_cli import kanban_db
from hermes_cli.kanban_spawn_provider import resolve_kanban_spawn


def test_default_provider_is_builtin():
    fn = resolve_kanban_spawn({"kanban": {}})
    assert fn is kanban_db._default_spawn
    assert getattr(fn, "_kanban_run_kind", None) is None


def test_temporal_selected_but_disabled_falls_back_to_builtin():
    cfg = {"kanban": {"spawn_provider": "temporal"}, "temporal": {"enabled": False}}
    fn = resolve_kanban_spawn(cfg)
    assert fn is kanban_db._default_spawn  # fell back


def test_temporal_selected_and_enabled_returns_tagged_callable(monkeypatch):
    # Stub the plugin import so this test doesn't depend on Task 7 wiring.
    import sys, types
    mod = types.ModuleType("plugins.kanban_spawn_temporal")
    def _spawn(task, workspace, *, board=None):  # noqa: ANN001
        return None
    mod.temporal_kanban_spawn = _spawn
    monkeypatch.setitem(sys.modules, "plugins.kanban_spawn_temporal", mod)
    cfg = {"kanban": {"spawn_provider": "temporal"}, "temporal": {"enabled": True}}
    fn = resolve_kanban_spawn(cfg)
    assert getattr(fn, "_kanban_run_kind", None) == "temporal"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `scripts/run_tests.sh tests/kanban/test_kanban_spawn_provider.py`
Expected: FAIL with `ModuleNotFoundError: No module named 'hermes_cli.kanban_spawn_provider'`.

- [ ] **Step 3: Add the config default**

In `hermes_cli/config.py`, inside the `"kanban": { ... }` block (after `"dispatch_interval_seconds": 60,` at `:2393`), add:

```python
        # Worker spawn backend. "builtin" (default) Popens `hermes chat`
        # locally and tracks it by PID. "temporal" starts a durable
        # KanbanTaskWorkflow per card so the worker survives a host/gateway
        # crash (requires temporal.enabled + a reachable Temporal server;
        # falls back to "builtin" otherwise). See docs/temporal/.
        "spawn_provider": "builtin",
```

- [ ] **Step 4: Implement the resolver**

```python
# hermes_cli/kanban_spawn_provider.py
"""Resolve the kanban worker spawn backend (cron-style provider seam)."""
from __future__ import annotations
import logging
from typing import Callable, Optional

log = logging.getLogger(__name__)


def resolve_kanban_spawn(cfg: Optional[dict] = None) -> Callable:
    """Return the spawn callable for the configured ``kanban.spawn_provider``.

    Default/fallback is the builtin subprocess spawn. ``temporal`` requires
    ``temporal.enabled`` true; otherwise (or on import failure) we log and
    fall back to builtin so kanban is never left without a spawn.
    The returned callable always accepts ``(task, workspace, *, board=None)``.
    """
    from hermes_cli.kanban_db import _default_spawn

    if cfg is None:
        from hermes_cli.config import load_config
        cfg = load_config()
    provider = ((cfg.get("kanban") or {}).get("spawn_provider") or "builtin").strip().lower()
    if provider == "builtin":
        return _default_spawn
    if provider == "temporal":
        if not bool((cfg.get("temporal") or {}).get("enabled")):
            log.warning(
                "kanban.spawn_provider=temporal but temporal.enabled is false; "
                "falling back to builtin subprocess spawn.")
            return _default_spawn
        try:
            from plugins.kanban_spawn_temporal import temporal_kanban_spawn
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "kanban.spawn_provider=temporal but the plugin failed to load (%s); "
                "falling back to builtin subprocess spawn.", exc)
            return _default_spawn
        temporal_kanban_spawn._kanban_run_kind = "temporal"  # type: ignore[attr-defined]
        return temporal_kanban_spawn
    log.warning("unknown kanban.spawn_provider=%r; using builtin.", provider)
    return _default_spawn
```

- [ ] **Step 5: Run the tests**

Run: `scripts/run_tests.sh tests/kanban/test_kanban_spawn_provider.py`
Expected: PASS (all three).

- [ ] **Step 6: Commit**

```bash
git add hermes_cli/kanban_spawn_provider.py hermes_cli/config.py tests/kanban/test_kanban_spawn_provider.py
git commit -m "feat(kanban): kanban.spawn_provider config + resolve_kanban_spawn seam

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 5: Wire the resolver into the dispatch tick

Replace the hardcoded `_default_spawn` fallback in `_dispatch_once_locked` (both the ready-dispatch and review-dispatch call sites) with `resolve_kanban_spawn`, and route the post-spawn bookkeeping to `_mark_run_temporal` for temporal runs vs `_set_worker_pid` for builtin.

**Files:**
- Modify: `hermes_cli/kanban_db.py:6901-6916` and `:6999-7016` (the two `_spawn = spawn_fn if spawn_fn is not None else _default_spawn` sites).
- Test: `tests/kanban/test_kanban_spawn_provider.py` (append an integration test).

**Interfaces:**
- Consumes: `resolve_kanban_spawn` (Task 4), `_mark_run_temporal` (Task 2), `_set_worker_pid` (existing).

- [ ] **Step 1: Write the failing test**

```python
# tests/kanban/test_kanban_spawn_provider.py  (append)
def test_dispatch_marks_run_kind_temporal_when_provider_tagged(kanban_conn, monkeypatch):
    from hermes_cli import kanban_db

    # A fake temporal spawn: returns no pid, tagged temporal.
    def fake_spawn(task, workspace, *, board=None):
        return None
    fake_spawn._kanban_run_kind = "temporal"
    monkeypatch.setattr(kanban_db, "resolve_kanban_spawn", lambda cfg=None: fake_spawn)

    # Seed one ready+assigned task and dispatch with default spawn_fn (None).
    kanban_db.add_task(kanban_conn, id="t-disp", title="x", assignee="default")
    with kanban_db.write_txn(kanban_conn):
        kanban_conn.execute("UPDATE tasks SET status='ready' WHERE id='t-disp'")
    kanban_db.dispatch_once(kanban_conn)  # spawn_fn=None → resolver path

    row = kanban_conn.execute(
        "SELECT status, run_kind, worker_pid FROM tasks WHERE id='t-disp'").fetchone()
    assert row["run_kind"] == "temporal"
    assert row["worker_pid"] is None
```

> Reuse the suite's dispatch test fixtures for seeding a dispatchable task (search `tests/kanban/test_kanban_dispatch_lock.py` / `test_kanban_per_profile_cap.py` for how they construct a ready/assigned task + call `dispatch_once`). Adjust seeding to satisfy `claim_task` (assignee must map to a spawnable profile, or stub `profile_exists`).

- [ ] **Step 2: Run test to verify it fails**

Run: `scripts/run_tests.sh tests/kanban/test_kanban_spawn_provider.py::test_dispatch_marks_run_kind_temporal_when_provider_tagged`
Expected: FAIL — `run_kind` is None (dispatch still hardcodes `_default_spawn`).

- [ ] **Step 3: Change both call sites**

At `:6901` and `:6999`, change the fallback:

```python
        _spawn = spawn_fn if spawn_fn is not None else resolve_kanban_spawn()
```

Add the import near the top of the function or module (module-level is fine — it's a cheap local import inside the resolver). At module top with the other `from hermes_cli....` imports, add:

```python
from hermes_cli.kanban_spawn_provider import resolve_kanban_spawn
```

> Guard against an import cycle: `kanban_spawn_provider` imports `kanban_db._default_spawn` *inside* its function (lazy), so a module-level import of `resolve_kanban_spawn` into `kanban_db` is safe. Verify `scripts/run_tests.sh tests/kanban/` still imports cleanly after adding it; if a cycle appears, move the import inside `_dispatch_once_locked`.

At both post-spawn blocks (`:6915` and `:7015`), replace:

```python
            if pid:
                _set_worker_pid(conn, claimed.id, int(pid))
```

with:

```python
            if getattr(_spawn, "_kanban_run_kind", None) == "temporal":
                _mark_run_temporal(conn, claimed.id)
            elif pid:
                _set_worker_pid(conn, claimed.id, int(pid))
```

- [ ] **Step 4: Run the tests**

Run: `scripts/run_tests.sh tests/kanban/test_kanban_spawn_provider.py`
Expected: PASS.

- [ ] **Step 5: Full kanban regression**

Run: `scripts/run_tests.sh tests/kanban/`
Expected: PASS — default config keeps `spawn_provider="builtin"` so the resolver returns `_default_spawn` and existing behavior is unchanged.

- [ ] **Step 6: Commit**

```bash
git add hermes_cli/kanban_db.py tests/kanban/test_kanban_spawn_provider.py
git commit -m "feat(kanban): dispatch via resolve_kanban_spawn; mark run_kind for temporal spawns

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 6: `run_kanban_worker` activity

The activity that Popens the card's subprocess inside the worker, heartbeats while it runs, and reaps the exit code. Lives with the other Temporal activities.

**Files:**
- Modify: `plugins/temporal/activities.py`
- Test: `tests/temporal/test_kanban_workflow.py` (activity-level part)

**Interfaces:**
- Consumes: `kanban_db._popen_from_spawn_args` (Task 1), `kanban_db.reap_temporal_worker` (Task 3), `kanban_db.connect` (existing — confirm the connection helper name with `grep -n "def connect" hermes_cli/kanban_db.py`).
- Produces: an activity `run_kanban_worker(payload: dict) -> dict` registered via `_make_activities()`. `payload = {"task_id": str, "spawn_args": dict, "board": Optional[str], "poll_seconds": int}`. Returns `{"exit_code": int, "reap": str}`.

- [ ] **Step 1: Write the failing test (activity in isolation)**

```python
# tests/temporal/test_kanban_workflow.py
import pytest
pytest.importorskip("temporalio")


def test_run_kanban_worker_popens_and_reaps(tmp_path, monkeypatch):
    from plugins.temporal import activities as A
    from hermes_cli import kanban_db

    calls = {}

    class FakeProc:
        pid = 999
        def __init__(self): self._n = 0
        def poll(self):
            self._n += 1
            return None if self._n < 2 else 0   # alive once, then exit 0

    monkeypatch.setattr(kanban_db, "_popen_from_spawn_args", lambda args: FakeProc())
    monkeypatch.setattr(kanban_db, "reap_temporal_worker",
                        lambda conn, tid, code, **kw: calls.setdefault("reap", (tid, code)) or "terminal")
    monkeypatch.setattr(kanban_db, "connect", lambda *a, **k: object())

    run = A._make_run_kanban_worker(heartbeat=lambda *a, **k: None, sleep=lambda s: None)
    out = run({"task_id": "t-1", "spawn_args": {"argv": []}, "board": None, "poll_seconds": 0})
    assert out["exit_code"] == 0
    assert out["reap"] == "terminal"
    assert calls["reap"] == ("t-1", 0)
```

> The activity is factored as `_make_run_kanban_worker(heartbeat, sleep)` so the polling loop is testable without a real Temporal context. The registered activity passes `activity.heartbeat` and `time.sleep`.

- [ ] **Step 2: Run test to verify it fails**

Run: `scripts/run_tests.sh tests/temporal/test_kanban_workflow.py::test_run_kanban_worker_popens_and_reaps`
Expected: FAIL with `AttributeError: module 'plugins.temporal.activities' has no attribute '_make_run_kanban_worker'`.

- [ ] **Step 3: Implement the activity**

Add to `plugins/temporal/activities.py` (read the file first to match the existing `_make_activities()` pattern and how `execute_durable_step`/`fire_cron_job` are registered):

```python
def _make_run_kanban_worker(heartbeat=None, sleep=None):
    """Factory so the poll loop is unit-testable without a Temporal context."""
    import time as _time
    from hermes_cli import kanban_db

    _sleep = sleep if sleep is not None else _time.sleep

    def _run(payload: dict) -> dict:
        _hb = heartbeat
        if _hb is None:
            from temporalio import activity
            _hb = activity.heartbeat
        task_id = payload["task_id"]
        board = payload.get("board")
        poll = int(payload.get("poll_seconds", 5))
        proc = kanban_db._popen_from_spawn_args(payload["spawn_args"])
        while proc.poll() is None:
            _hb({"task_id": task_id, "pid": getattr(proc, "pid", None)})
            _sleep(poll)
        exit_code = int(proc.poll() or 0)
        conn = kanban_db.connect(board=board)
        try:
            reap = kanban_db.reap_temporal_worker(conn, task_id, exit_code, board=board)
        finally:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
        return {"exit_code": exit_code, "reap": reap}

    return _run
```

In `_make_activities()`, register the activity using the same decorator/wrapper the other activities use (e.g. `activity.defn(name="run_kanban_worker")`). Read how `fire_cron_job` was registered in Phase 4a and mirror it exactly:

```python
    run_kanban_worker = activity.defn(name="run_kanban_worker")(
        lambda payload: _make_run_kanban_worker()(payload)
    )
    # ... include run_kanban_worker in the returned activities list
```

> Match the real registration idiom in this file — if activities are defined as module-level `@activity.defn` functions plus a list, define `run_kanban_worker` that way instead and have it call `_make_run_kanban_worker()`. The factory is the unit-tested core; the registration is the thin Temporal binding. Confirm `kanban_db.connect`'s real signature (board kwarg) and adjust.

- [ ] **Step 4: Run the test**

Run: `scripts/run_tests.sh tests/temporal/test_kanban_workflow.py::test_run_kanban_worker_popens_and_reaps`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add plugins/temporal/activities.py tests/temporal/test_kanban_workflow.py
git commit -m "feat(temporal): run_kanban_worker activity — supervise + reap a kanban subprocess

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 7: `KanbanTaskWorkflow` + temporal spawn_fn + worker registration

The workflow that wraps the activity with retry/timeout, the `spawn_fn` that starts it (with per-tick fallback), and worker registration. Grouped because they form one vertical slice and share the workflow name as their only interface.

**Files:**
- Modify: `plugins/temporal/workflows.py` (add `KanbanTaskWorkflow` + `_make_kanban_task_workflow`)
- Create: `plugins/kanban_spawn_temporal/__init__.py`
- Modify: `plugins/temporal/worker.py` (register both)
- Test: `tests/temporal/test_kanban_workflow.py` (append workflow-config + spawn-fallback tests)

**Interfaces:**
- Consumes: `run_kanban_worker` activity name (Task 6); `kanban_db.build_spawn_args` + `_default_spawn` (Task 1); `plugins.temporal.client.connect`, `tconfig.resolve_temporal_config`, `load_config` (existing).
- Produces:
  - `KanbanTaskWorkflow` (module-level in the `try: from temporalio import workflow` block) + `_make_kanban_task_workflow()` in both branches, matching the existing pattern for `CronFireWorkflow`/`DurableRunWorkflow`.
  - `temporal_kanban_spawn(task, workspace, *, board=None) -> Optional[int]` in `plugins/kanban_spawn_temporal/__init__.py`. Returns `None` on success (so the dispatcher leaves `worker_pid` NULL and marks `run_kind='temporal'`); on connect/start failure, logs and returns `_default_spawn(task, workspace, board=board)` (per-tick fallback). Workflow id: `f"hermes-kanban-{task.id}-{task.current_run_id}"`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/temporal/test_kanban_workflow.py  (append)
def test_temporal_spawn_falls_back_on_connect_error(tmp_path, monkeypatch):
    from plugins import kanban_spawn_temporal
    from hermes_cli import kanban_db

    fell_back = {}
    monkeypatch.setattr(kanban_db, "_default_spawn",
                        lambda task, ws, *, board=None: fell_back.setdefault("pid", 4242) or 4242)
    # Force the start path to raise (no server).
    monkeypatch.setattr(kanban_spawn_temporal, "_start_kanban_workflow",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no server")))

    class T:
        id = "t-x"; current_run_id = 1; assignee = "default"
    pid = kanban_spawn_temporal.temporal_kanban_spawn(T(), str(tmp_path), board=None)
    assert pid == 4242   # fell back to builtin for this tick
    assert fell_back["pid"] == 4242


def test_kanban_workflow_retry_tracks_failure_limit(monkeypatch):
    from plugins.temporal import workflows
    wf = workflows._make_kanban_task_workflow()
    # The factory exposes the retry/timeout it would use for a given payload.
    policy = workflows._kanban_retry_policy(failure_limit=3)
    assert policy.maximum_attempts == 4   # 1 + failure_limit
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `scripts/run_tests.sh tests/temporal/test_kanban_workflow.py`
Expected: FAIL — `ModuleNotFoundError: plugins.kanban_spawn_temporal` / missing `_kanban_retry_policy`.

- [ ] **Step 3: Add the workflow**

In `plugins/temporal/workflows.py`, read the existing `CronFireWorkflow` block (added in Phase 4a) and mirror it. Add a shared retry helper and the workflow:

```python
def _kanban_retry_policy(failure_limit: int):
    from temporalio.common import RetryPolicy as _RetryPolicy
    return _RetryPolicy(maximum_attempts=1 + int(failure_limit))
```

Inside the `try: from temporalio import workflow as _wf ...` block, add:

```python
    @_wf.defn(name="KanbanTaskWorkflow")
    class KanbanTaskWorkflow:
        @_wf.run
        async def run(self, payload: dict) -> dict:
            from datetime import timedelta
            spawn_args = payload["spawn_args"]
            max_runtime = spawn_args.get("max_runtime_seconds") or 3600
            return await _wf.execute_activity(
                "run_kanban_worker",
                {
                    "task_id": payload["task_id"],
                    "spawn_args": spawn_args,
                    "board": payload.get("board"),
                    "poll_seconds": payload.get("poll_seconds", 5),
                },
                start_to_close_timeout=timedelta(seconds=int(max_runtime) + 60),
                heartbeat_timeout=timedelta(seconds=int(payload.get("poll_seconds", 5)) * 6 + 30),
                retry_policy=_kanban_retry_policy(payload.get("failure_limit", 2)),
            )
```

Provide `_make_kanban_task_workflow()` in both the `try` and `except ImportError` branches, matching how `_make_cron_fire_workflow` is structured (returns the class in the real branch, a stub/`None` in the import-less branch).

- [ ] **Step 4: Add the temporal spawn_fn**

```python
# plugins/kanban_spawn_temporal/__init__.py
"""Temporal-backed kanban worker spawn: start a durable KanbanTaskWorkflow
per claimed card instead of a local subprocess. Selected via
kanban.spawn_provider=temporal (see hermes_cli/kanban_spawn_provider.py)."""
from __future__ import annotations
import asyncio
import logging
from typing import Optional

log = logging.getLogger(__name__)


def _start_kanban_workflow(task, workspace, board) -> None:
    from hermes_cli.config import load_config
    from hermes_cli.kanban_db import build_spawn_args
    from plugins.temporal.tconfig import resolve_temporal_config
    from plugins.temporal.client import connect

    s = resolve_temporal_config(load_config())
    spawn_args = build_spawn_args(task, workspace, board=board)
    failure_limit = int((load_config().get("kanban") or {}).get("failure_limit", 2))
    wf_id = f"hermes-kanban-{task.id}-{task.current_run_id}"

    async def _go():
        client = await connect(s)
        await client.start_workflow(
            "KanbanTaskWorkflow",
            {
                "task_id": task.id,
                "spawn_args": spawn_args,
                "board": board,
                "failure_limit": failure_limit,
            },
            id=wf_id,
            task_queue=s.task_queue,
        )

    asyncio.run(_go())


def temporal_kanban_spawn(task, workspace, *, board: Optional[str] = None):
    """Start a durable KanbanTaskWorkflow for this card. Returns None on
    success (dispatcher leaves worker_pid NULL + marks run_kind='temporal').
    On any failure to reach Temporal, falls back to the builtin subprocess
    spawn FOR THIS TICK so the card still runs (non-durably)."""
    from hermes_cli.kanban_db import _default_spawn

    try:
        _start_kanban_workflow(task, workspace, board)
        return None
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "temporal kanban spawn failed for task %s (%s); "
            "falling back to builtin subprocess spawn for this tick.",
            getattr(task, "id", "?"), exc)
        return _default_spawn(task, workspace, board=board)
```

- [ ] **Step 5: Register on the worker**

In `plugins/temporal/worker.py`, where `run_worker` lists workflows and activities (read the existing registration for `CronFireWorkflow`/`fire_cron_job`), add `KanbanTaskWorkflow` to the `workflows=[...]` and `run_kanban_worker` to the `activities=[...]`. Mirror the Phase 4a edit exactly.

- [ ] **Step 6: Run the tests**

Run: `scripts/run_tests.sh tests/temporal/test_kanban_workflow.py`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add plugins/temporal/workflows.py plugins/temporal/worker.py plugins/kanban_spawn_temporal/__init__.py tests/temporal/test_kanban_workflow.py
git commit -m "feat(temporal): KanbanTaskWorkflow + temporal kanban spawn_fn + worker registration

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 8: Gated end-to-end test (time-skipping)

A full-loop test against the time-skipping `WorkflowEnvironment`: a claimed card → `KanbanTaskWorkflow` → `run_kanban_worker` (stubbed Popen) → card reaches `done`; and a confirmation that `release_stale_claims`/`detect_stale_running` do not reclaim while the run is temporal.

**Files:**
- Test: `tests/temporal/test_kanban_workflow.py` (append the e2e)

**Interfaces:**
- Consumes: everything from Tasks 1–7. Uses `temporalio.testing.WorkflowEnvironment` like the Phase 4a cron e2e (read `tests/temporal/` for the existing time-skipping harness and reuse it).

- [ ] **Step 1: Write the gated e2e test**

```python
# tests/temporal/test_kanban_workflow.py  (append)
import pytest
pytest.importorskip("temporalio")


@pytest.mark.asyncio
async def test_kanban_workflow_runs_activity_and_completes(monkeypatch):
    from temporalio.testing import WorkflowEnvironment
    from temporalio.worker import Worker
    from plugins.temporal import activities as A
    from plugins.temporal.workflows import _make_kanban_task_workflow
    from hermes_cli import kanban_db

    # Stub the subprocess: exits 0 immediately; reap returns terminal.
    class FakeProc:
        pid = 1
        def poll(self): return 0
    monkeypatch.setattr(kanban_db, "_popen_from_spawn_args", lambda args: FakeProc())
    monkeypatch.setattr(kanban_db, "connect", lambda *a, **k: object())
    seen = {}
    monkeypatch.setattr(kanban_db, "reap_temporal_worker",
                        lambda conn, tid, code, **kw: seen.setdefault("code", code) or "terminal")

    WF = _make_kanban_task_workflow()
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(env.client, task_queue="tq",
                          workflows=[WF],
                          activities=A._make_activities()):
            result = await env.client.execute_workflow(
                "KanbanTaskWorkflow",
                {"task_id": "t-1", "spawn_args": {"argv": []}, "board": None,
                 "failure_limit": 2, "poll_seconds": 0},
                id="hermes-kanban-t-1-1", task_queue="tq")
    assert result["exit_code"] == 0
    assert result["reap"] == "terminal"
    assert seen["code"] == 0
```

> If `_make_activities()` requires arguments (it bootstraps tool discovery / takes config in earlier phases), read its current signature and pass what the cron e2e passes. The point of this test is the workflow→activity wiring, so stub external IO.

- [ ] **Step 2: Run the e2e**

Run: `scripts/run_tests.sh tests/temporal/test_kanban_workflow.py`
Expected: PASS (skips cleanly if `temporalio` not installed).

- [ ] **Step 3: Commit**

```bash
git add tests/temporal/test_kanban_workflow.py
git commit -m "test(temporal): gated e2e for KanbanTaskWorkflow → run_kanban_worker

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 9: Documentation

Document the opt-in setting and its semantics/limitation in AGENTS.md, alongside the existing Temporal section.

**Files:**
- Modify: `AGENTS.md` (Temporal section)

- [ ] **Step 1: Add the docs**

Find the Temporal section in `AGENTS.md` (`grep -n "Temporal" AGENTS.md`) and append, after the cron-provider (Phase 4a) note:

```markdown
- **Durable kanban workers (Phase 4b).** Set `kanban.spawn_provider: temporal`
  (default `builtin`) to run each claimed card's `hermes chat` worker inside a
  durable `KanbanTaskWorkflow` on the `hermes temporal worker`, instead of a local
  subprocess. The worker survives a host/gateway crash — Temporal re-runs the
  activity on return. SQLite still owns claim/promote/dependency/circuit-breaker;
  Temporal is the sole supervisor for its runs, so the TTL/heartbeat reclaimers
  skip `run_kind='temporal'` rows (the activity reaps its own subprocess via
  `reap_temporal_worker`). Falls back to the builtin subprocess spawn if
  `temporal.enabled` is false or Temporal is unreachable — kanban is never left
  without a spawn. **Limitation:** the activity Popens on the worker host, so the
  worker must reach the card's `workspace_path` (same host or shared FS);
  cross-host workspaces are a flagged follow-up.
```

- [ ] **Step 2: Commit**

```bash
git add AGENTS.md
git commit -m "docs(temporal): document kanban.spawn_provider=temporal (Phase 4b)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Final verification

- [ ] **Run the whole touched surface:**

Run: `scripts/run_tests.sh tests/kanban/ tests/temporal/`
Expected: all pass (temporal e2e skips without the extra).

- [ ] **Lint:**

Run: `ruff check hermes_cli/kanban_db.py hermes_cli/kanban_spawn_provider.py plugins/kanban_spawn_temporal/ plugins/temporal/`
Expected: clean (only PLW1514 is enabled).

- [ ] **Default-config sanity:** confirm `resolve_kanban_spawn()` returns `_default_spawn` under the shipped config (no behavior change for users who don't opt in).

---

## Self-Review notes (author)

**Spec coverage:** durable spawn behind existing seam (Tasks 1,4,5,7) ✓; subprocess-inside-activity with heartbeat (Task 6) ✓; sole-supervisor / no double-execution via `run_kind` skip (Task 2) + activity self-reap (Task 3) ✓; retry = `1+failure_limit` (Task 7) ✓; resolver fallback + per-tick fallback (Tasks 4,7) ✓; built-in default / inert when unselected (Task 4 default, Task 5 regression) ✓; workspace-locality limitation documented (Task 9) ✓; gated e2e (Task 8) ✓.

**Type consistency:** `build_spawn_args` dict keys (`argv/cwd/env_overlay/log_path/max_runtime_seconds`) are consumed identically by `_popen_from_spawn_args` (Task 1), the activity (Task 6), and the spawn_fn (Task 7). `_kanban_run_kind` tag set in Task 4, read in Task 5. `temporal_kanban_spawn` signature `(task, workspace, *, board=None)` matches the resolver's contract and the dispatch call shim.

**Known integration risks to verify during execution (not placeholders — explicit checks):** (1) confirm `_record_task_failure`'s exact kwargs before Task 3; (2) confirm `kanban_db.connect`'s signature/board kwarg before Task 6; (3) confirm `_make_activities()` signature before Tasks 6/8; (4) confirm `KANBAN_RATE_LIMIT_EXIT_CODE` constant name before Task 3; (5) watch for an import cycle when adding the module-level `resolve_kanban_spawn` import in Task 5 (fallback: import inside the function).
