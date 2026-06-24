---
name: recursive-language-model
description: |
  Use the `rlm` tool to attack tasks over very long or large contexts (huge logs,
  transcripts, document sets) by delegating to fast-rlm — a Recursive Language Model
  that drives a code REPL to explore/slice the context and spawns sub-agents whose
  results never enter your context.
version: 1.0.0
platforms: [macos, linux]
metadata:
  hermes:
    tags: [rlm, long-context, recursion, fast-rlm]
    related_skills: []
---

# Recursive Language Model (fast-rlm)

You have an `rlm` tool. Reach for it when a task spans **far more text than is worth
reading into your own context** — multi-megabyte logs, long transcripts, large
document collections, "find/aggregate X across all of this" questions.

## When to use vs. not

- **Use `rlm`** when the answer requires scanning/aggregating across a body of text
  too large to read directly, or when you'd otherwise burn your context window on
  raw content.
- **Don't use `rlm`** for short inputs you can just read, or for tasks that need
  your own tools/memory — the RLM sub-agents are isolated.

## How to call it

- `query`: the task/question (always required).
- Provide the long content via **exactly one** of:
  - `context`: inline text (it is written to a temp file in the backend, so size is fine), or
  - `input_path`: a path to a file already in the active environment.
- Optional: `primary_agent` / `sub_agent` to override the model, `max_global_calls`
  for the budget.

The tool returns `{"status", "result", "usage", "log_path"}`. Inspect a run's
reasoning later with `viewlog <log_path>` / `fast-rlm-log <log_path>`.

## Prerequisites

- **Deno 2+** must be installed (fast-rlm runs its REPL on Deno/Pyodide):
  `curl -fsSL https://deno.land/install.sh | sh`.
- **fast-rlm** is auto-installed (pinned PyPI) on first use. To use a local
  checkout instead, set `rlm.engine_path` in config.yaml — it is installed
  **non-editable** (an editable `-e` install breaks fast-rlm's import because
  the checkout ships both `fast_rlm.py` and a `fast_rlm/` package). A checkout
  you've already installed yourself is used as-is.
- Credentials: the tool reuses Hermes' **active provider** automatically — no
  separate key to set. Override the model with `rlm.primary_agent` in config.yaml.

## Remote backends & key safety

fast-rlm makes its own LLM calls, so your LLM key is injected into the execution
backend. On **local/docker-on-host** it stays on your machine. On **cloud sandboxes
(modal/daytona)** it would transit to that sandbox, so it is **blocked by default**.
Enable it deliberately with:

```yaml
rlm:
  allow_remote_backends: true
```

For remote backends, ensure the image has Deno + fast-rlm (`pip install fast-rlm`
plus the Deno install one-liner) — the host availability check can't guarantee it.

**Known limitation:** during staging, the LLM key is written into the sandbox via a
base64-encoded shell command, so it is briefly present (base64-encoded, not encrypted)
in the execution backend's process arguments. On local/docker-on-host this stays on
your machine; cloud backends are gated off by default (`allow_remote_backends: false`).
Avoid running the `rlm` tool in a backend whose process list is visible to untrusted users.

## Config knobs (config.yaml)

```yaml
rlm:
  primary_agent: null          # default: Hermes' active model
  sub_agent: null              # default: primary_agent
  max_global_calls: 50
  timeout_seconds: 600
  allow_remote_backends: false
  engine_path: null            # abs path to a fast-rlm checkout to use instead of PyPI
```
