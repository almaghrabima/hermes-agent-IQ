# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Read AGENTS.md first

`AGENTS.md` (~70KB) is the canonical development guide and contains the full
detail behind everything summarized here: the contribution rubric, the
"Footprint Ladder" for adding capability, plugin/skill/toolset authoring
standards, profile-safety rules, and known pitfalls. This file is the fast
orientation; AGENTS.md is the reference. When the two ever disagree, AGENTS.md wins.

## What Hermes is

A personal, self-improving AI agent. A single agent core (`run_agent.py`) runs
across a CLI (`cli.py`), a messaging gateway (~20 platforms), an Ink/React TUI
(`ui-tui/` + `tui_gateway/`), and an Electron desktop app (`apps/desktop/`).
It learns across sessions (memory + skills), spawns subagents, runs scheduled
cron jobs, and drives real terminals and browsers. Capability is added at the
**edges** (plugins, skills, CLI commands), not by growing the core.

## Two invariants that gate almost every change

1. **Per-conversation prompt caching is sacred.** A long conversation reuses a
   cached prefix every turn. Anything that mutates past context, swaps toolsets,
   or rebuilds the system prompt mid-conversation invalidates the cache and
   multiplies user cost. Don't do it (the sole exception is context compression).
   Keep strict message role alternation (never two same-role messages in a row;
   no synthetic user message injected mid-loop) and a byte-stable system prompt
   for the life of a conversation.
2. **The core is a narrow waist.** Every model tool ships on every API call, so
   the bar for a new *core tool* is very high. Prefer, in order: extend existing
   code → CLI command + skill → service-gated tool (`check_fn`) → plugin → MCP
   server → new core tool (last resort). The product is expansive at the edges
   (new platforms/providers/models/features land routinely) and conservative at
   the waist.

## Common commands

```bash
# Setup (from a full git checkout)
uv venv .venv --python 3.11 && source .venv/bin/activate
uv pip install -e ".[all,dev]"

# Tests — ALWAYS use the wrapper, never bare `pytest`. It enforces CI parity:
# unsets credential env vars, TZ=UTC, LANG=C.UTF-8, subprocess-per-test isolation.
scripts/run_tests.sh                                   # full suite
scripts/run_tests.sh tests/gateway/                    # one directory
scripts/run_tests.sh tests/agent/test_foo.py::test_x   # single test
scripts/run_tests.sh --no-isolate tests/foo/           # faster, for debugging
scripts/run_tests.sh -v --tb=long                      # pass-through pytest flags

# Lint / typecheck (dev extra installs ruff + ty)
ruff check .          # only PLW1514 (unspecified-encoding) is enabled — see pyproject.toml
ty check              # type checker

# Node workspaces (TUI, web dashboard, desktop)
npm run install:tui && npm run install:web && npm run install:desktop
```

Python is pinned to `>=3.11,<3.14` (the `<3.14` ceiling is load-bearing — see
the comment in `pyproject.toml`). Direct dependencies are **exact-pinned**
(`==X.Y.Z`); provider/backend-specific packages live in optional extras and are
lazy-installed at first use via `tools/lazy_deps.py`, not in `[all]`.

## Architecture map (the load-bearing files)

```
run_agent.py        AIAgent class + the synchronous conversation loop (~12k LOC)
model_tools.py      Tool orchestration: discover_builtin_tools(), handle_function_call()
toolsets.py         Toolset definitions, _HERMES_CORE_TOOLS list
cli.py              HermesCLI — interactive CLI orchestrator (~11k LOC)
hermes_state.py     SessionDB — SQLite session store with FTS5 search
hermes_constants.py get_hermes_home() / display_hermes_home() — profile-aware paths
agent/              Provider adapters, memory, prompt caching, compression, context engine
hermes_cli/         CLI subcommands, setup wizard, plugin loader, skin engine, commands.py
tools/              Tool implementations — auto-discovered via tools/registry.py
  environments/     Terminal backends: local, docker, ssh, modal, daytona, singularity
gateway/            Messaging gateway: run.py + session.py + platforms/<platform>.py
plugins/            Plugin system: memory/, context_engine/, model-providers/, kanban/, ...
skills/             Built-in skills (optional-skills/ = shipped but inactive by default)
cron/               Scheduler: jobs.py, scheduler.py
ui-tui/ tui_gateway/  Ink (React) TUI + its Python JSON-RPC backend
acp_adapter/        ACP server (VS Code / Zed / JetBrains integration)
```

**Tool import chain:** `tools/registry.py` (no deps) ← `tools/*.py` (each calls
`registry.register()` at import) ← `model_tools.py` (triggers discovery) ←
`run_agent.py` / `cli.py` / `batch_runner.py`.

**The agent loop** lives in `AIAgent.run_conversation()` (`run_agent.py`) — fully
synchronous, with interrupt checks, iteration/budget tracking, and a one-turn
grace call. Messages use OpenAI format (`{"role": system/user/assistant/tool}`);
reasoning content is stored in `assistant_msg["reasoning"]`.

**Slash commands** are defined once in the `COMMAND_REGISTRY` list in
`hermes_cli/commands.py`; CLI, TUI, and gateway all derive from it. Skill slash
commands (`agent/skill_commands.py`) are injected as a **user message**, never
the system prompt, to preserve prompt caching.

## Configuration: config.yaml vs .env

- **`~/.hermes/config.yaml`** — ALL behavioral settings (timeouts, thresholds,
  feature flags, display prefs).
- **`~/.hermes/.env`** — **secrets only** (API keys, tokens, passwords).

Do **not** add new `HERMES_*` env vars for non-secret config — that's a rejected
pattern. New user-facing settings go in `config.yaml` and integrate with the
existing UX (`hermes tools`, `hermes setup`), not a raw env var.

Paths are **profile-aware**: never hardcode `~/.hermes`. Always resolve via
`get_hermes_home()` / `display_hermes_home()` from `hermes_constants.py` so
multi-instance profiles work.

## Testing conventions

- Every test runs in a freshly-spawned `spawn` subprocess (`tests/_isolate_plugin.py`),
  so module-level dicts/sets and ContextVars cannot leak between tests. The
  isolation plugin auto-loads from `addopts` in `pyproject.toml` even under bare
  `pytest`. Each test is capped at 30s.
- Integration tests (marked `integration`) are excluded by default (`addopts = -m 'not integration'`).
- Tests must run against a temp `HERMES_HOME` — **never write to the real `~/.hermes/`**.
- **No change-detector tests** — don't assert on data expected to change (model
  catalogs, config version literals, enumeration counts). Assert invariants/contracts
  (how two pieces of data must relate), not frozen snapshots.
- For anything touching resolution chains, config propagation, security
  boundaries, or remote backends, exercise the real path against a temp
  `HERMES_HOME` rather than relying on mocks.

## TypeScript (desktop, TUI, website)

Nanostores over component state for shared state; each feature owns its atoms
(chat state near chat, shared atoms in `src/store`, pure helpers in `src/lib`).
Render-from-atom uses `useStore`; non-rendering reads use `$atom.get()`. Keep
route roots thin, no monolithic "god" hooks, prefer `interface` for public props
and extend React primitives (`React.ComponentProps<'button'>`). Async handlers
make intent explicit with `void`: `onClick={() => void save()}`.
