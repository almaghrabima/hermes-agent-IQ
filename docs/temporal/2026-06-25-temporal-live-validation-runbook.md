# Runbook: end-to-end live validation of the Temporal durable plugin (Phase 1.1)

**Date:** 2026-06-25
**Applies to:** `plugins/temporal/` (Phase 0+1, PR #17)
**Why this exists:** the automated suite validates the plugin **piecewise** — unit
tests for config/client/tools/gating, the real registry seam (`delegate_task`
lookup), the worker-thread approval callback, and a gated `DurableRunWorkflow`
e2e that uses a **fake** activity (temporalio time-skipping server). What is NOT
covered by CI is a **full worker → real subagent** run against a live Temporal
server with a real model. This runbook closes that gap. It cannot run in a
sandbox: it needs a running worker, a Temporal server, and a configured LLM.

See the design/plan: `docs/temporal/2026-06-25-temporal-phase0-1-design.md`
and `docs/temporal/2026-06-25-temporal-phase0-1-plan.md`.

## What this proves (that CI does not)

1. `hermes temporal worker` boots, bootstraps the builtin tool registry, and polls.
2. `durable_run` starts a `DurableRunWorkflow`, the worker executes each step as an
   activity, and each step runs a **real `delegate_task` subagent** (real LLM call)
   — no `KeyError: delegate_task`, no `input()` approval hang.
3. Retries/timeouts/exactly-once behave against the real server.
4. `durable_status` returns the completed result; long runs poll correctly.

## Prerequisites

- A checkout with the plugin (this branch/PR), and the extra installed:
  `uv pip install -e ".[temporal]"` (pins `temporalio==1.29.0`).
- The **Temporal CLI** for the local dev server:
  `brew install temporal` (macOS) or see https://docs.temporal.io/cli — provides
  `temporal server start-dev`. (Alternatively use Temporal Cloud; see below.)
- A working Hermes model/provider — run `hermes setup` and confirm `hermes` can hold
  a normal conversation. The durable step spawns a real subagent, so a model + key
  must be configured (`~/.hermes/config.yaml` + `~/.hermes/.env`).

## Step 1 — Configure the plugin

Enable the plugin and Temporal in `~/.hermes/config.yaml` (resolve via
`get_hermes_home()`; never hardcode the path):

```yaml
plugins:
  enabled: [temporal]            # standalone plugins are opt-in
temporal:
  enabled: true
  target: "localhost:7233"       # dev-server default
  namespace: "default"
  tls: false
  task_queue: "hermes"
  dev_server: true
  step_timeout_seconds: 600
  default_retry:
    max_attempts: 3
# For an unattended run where the subagent may hit dangerous commands, opt in:
delegation:
  subagent_auto_approve: false   # default = auto-DENY dangerous cmds (safe).
                                 # set true ONLY for trusted autonomous runs.
```

> Approval policy: durable steps install the configured non-interactive approval
> callback on the worker thread. Default **auto-deny** means a subagent that tries a
> dangerous command gets a refusal it can recover from (it will NOT prompt/hang).
> If your test step legitimately needs such a command, set
> `delegation.subagent_auto_approve: true`.

## Step 2 — Start the Temporal dev server

```bash
temporal server start-dev          # gRPC on :7233, Web UI on http://localhost:8233
```

Leave it running. Confirm: `temporal operator namespace list` lists `default`.

## Step 3 — Start the Hermes Temporal worker (separate terminal)

```bash
source .venv/bin/activate
hermes temporal worker
```

Expected: it connects to `localhost:7233` and begins polling the `hermes` task
queue. (Internally it calls `discover_builtin_tools()` so `delegate_task` is
registered before activities fire — the fix for the registry-bootstrap bug.)

If it prints `temporal.enabled is false …` your config from Step 1 isn't being read
(check the active profile / `HERMES_HOME`).

## Step 4 — Trigger a durable run

Two ways:

**(a) Through the agent (realistic):** start `hermes`, then ask it to use the tool,
e.g. *"Use durable_run with two steps: step 1 'list three colors', step 2 'count the
letters in those colors'."* The model calls `durable_run`.

**(b) Direct (deterministic):** run this from the activated venv at repo root:

```bash
python - <<'PY'
import json
from plugins.temporal import tools
out = tools.handle_durable_run({
    "steps": [
        {"name": "gen",   "prompt": "List exactly three fruits, comma-separated."},
        {"name": "count", "prompt": "How many letters are in the word 'banana'? Answer with a number."},
    ],
    "wait_seconds": 120,
})
print(json.dumps(json.loads(out), indent=2))
PY
```

Expected: `{"status": "completed", "run_id": "durable-…", "result": {"steps": [...],
"completed": 2}}`, with each step's `ok: true` and a real model answer in `result`.
If it returns `{"status": "running", "run_id": …}`, the run exceeded `wait_seconds`
— poll it:

```bash
python -c "import json; from plugins.temporal import tools; print(tools.handle_durable_status({'run_id':'durable-XXXX'}))"
```

## Step 5 — Confirm in the Temporal Web UI

Open http://localhost:8233 → Workflows → the `durable-…` run. Verify:
- Workflow type `DurableRunWorkflow`, status **Completed**.
- One `run_step` **activity per step**, each Completed (Failed→Completed if a retry occurred).
- Activity input is the step dict; output is `{"name","ok","result"}`.

## Step 6 — (optional) Prove retry/exactly-once live

Temporarily make a step fail transiently (e.g. point a step's prompt at a tool that
errors once) or kill+restart the worker mid-run: the workflow must resume and the
activity must not double-apply. The history in the UI shows the retry attempts and a
single successful completion.

## Temporal Cloud variant

Instead of the dev server:
```yaml
temporal:
  enabled: true
  target: "<namespace>.<account>.tmprl.cloud:7233"
  namespace: "<namespace>.<account>"
  tls: true
```
`.env`: `TEMPORAL_API_KEY=<key>`. (mTLS cert/key auth is **not** wired in Phase 1 —
API-key auth only; mTLS is future work.) Then run Steps 3–5 unchanged.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `durable_status` returns `pending` forever | No worker polling the `hermes` task queue. Start `hermes temporal worker` (Step 3). |
| Tools `durable_run`/`durable_status` not offered to the model | `check_fn` gated off: `temporal.enabled` false or server unreachable. Check Steps 1–2. |
| Worker exits: `temporal.enabled is false` | Wrong profile/`HERMES_HOME`; config not loaded. |
| `RuntimeError: delegate_task tool not registered` | The worker didn't run discovery — should not happen (it's wired in `run_worker`); verify you're on this branch. |
| Step result `ok:false` / refusal about a dangerous command | Auto-deny policy denied it. Set `delegation.subagent_auto_approve: true` for trusted runs. |
| `ImportError: temporalio is required …` | `uv pip install -e ".[temporal]"`. |
| `ModuleNotFoundError: temporalio` starting the worker | Same — extra not installed in the active venv. |

## Recording the result

When green, note it in the Phase 0+1 plan's evidence area (and/or the PR): host,
`temporalio` version, server (dev vs Cloud), number of steps, and that each step ran
a real subagent (UI shows `run_step` activities Completed). That flips the
"end-to-end live path unverified" caveat to confirmed.
