# fast-rlm Kernel Sandboxing (Phase 4) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Design:** `docs/rlm/2026-06-25-fast-rlm-kernel-phase4-per-os-boundaries-design.md`

**Goal:** Add **microVM-grade kernel runtimes** (Kata Containers, and Firecracker via Kata's FC backend) as opt-in values of the existing `kernel_runtime` knob, and make every non-`runc` runtime **fail cleanly with an actionable message** on hosts that can't support it (no `/dev/kvm`, runtime not registered) instead of surfacing a raw Docker error.

## Key realization (keeps this phase small)

`buildDockerArgs` (`src/kernel_client.ts`) **already forwards any non-`runc` runtime** as `--runtime <name>` (line 23–24). So:

- **Kata** is a registered Docker/OCI runtime (`kata-runtime` / `io.containerd.kata.v2`) → it already rides the existing passthrough. No engine arg-construction change is needed for it to *work*.
- **Firecracker** is **not** a drop-in `docker --runtime` value. The pragmatic Docker-compatible path is **Kata configured with its Firecracker (or Cloud Hypervisor) hypervisor backend** — surfaced as a distinct runtime name (e.g. a `kata-fc` Docker runtime alias). Plain `firecracker-containerd` is out of scope (not Docker-driven).

Therefore Phase 4's real value-add is **not** new arg plumbing — it is: (1) a **preflight** that turns "unknown runtime"/"no KVM" into a clear error, (2) light **config validation/allow-listing**, (3) **tests**, and (4) keeping docs honest. The Hermes-side guard (`kernel_sandbox: docker` ⇒ local backend only) already exists and is unchanged.

## Tech Stack

Deno/TypeScript (engine), Python 3.11 stdlib (Hermes `rlm_tool` + driver), Docker with an OCI runtime (Kata) registered. No real LLM in automated tests.

## Global Constraints

- **Non-breaking:** `kernel_runtime` default stays `runc`; `runsc`/`kata`/`kata-fc` are opt-in. `kernel_sandbox` default `local`, `docker` opt-in. `kernel_network` default `none`.
- **Default-deny egress preserved:** microVM runtimes run with `--network none`; `llm_query`/MCP still ride the stdio control channel to the host. No new network surface.
- **Clean-fail, never silent-downgrade:** selecting a microVM runtime on a host without `/dev/kvm` or without that runtime registered must error with a message naming the missing prerequisite — never fall back to `runc`/a weaker boundary.
- **No KVM on the dev host (macOS):** only arg-construction + preflight-logic **unit** tests run locally; the microVM **e2e** path is gated on KVM and validated only on a Linux/KVM host (mirrors how `runsc` e2e is gated today).
- Wire frames, kernel core (stdlib-only), and the pyodide / `local` / `runc` / `runsc` paths remain behaviorally unchanged.
- Deno at `~/.deno/bin` (prefix test commands with `export PATH="$HOME/.deno/bin:$PATH"`). Branch off `main`; commit there; do not push.

## File Structure

- Modify: `third_party/fast-rlm/src/kernel_client.ts` — add `preflightRuntime()`; call it in `Kernel.start` before `docker run` for non-`runc` runtimes; keep `buildDockerArgs` generic.
- Modify: `third_party/fast-rlm/src/config.ts`, `rlm_config.yaml`, `fast_rlm/_runner.py` — document/validate the expanded `kernel_runtime` value set (free-form string retained; add an optional known-values hint).
- Modify: `third_party/fast-rlm/tests/docker_launcher_test.ts` — arg-construction for `kata`/`kata-fc`; preflight clean-fail unit test; KVM-gated microVM e2e.
- Modify: `tools/rlm_tool.py` / `tools/rlm/_driver.py` — pass the new runtime values through unchanged (verify, add test).
- Modify: `tests/tools/test_rlm_tool.py`, `tests/tools/test_rlm_driver.py` — runtime passthrough coverage.
- Modify: `third_party/fast-rlm/CLAUDE.md` — note Kata/Firecracker-via-Kata are now recognized runtimes (boundary matrix already added in #4).

---

## Task 1: Runtime preflight in the engine

**Files:** Modify `third_party/fast-rlm/src/kernel_client.ts`, `third_party/fast-rlm/tests/docker_launcher_test.ts`

**Interface produced:** `preflightRuntime(runtime: string): Promise<void>` — no-op for `runc`; for any other value it (a) checks the runtime is registered (`docker info`'s runtimes), and on Linux (b) checks `/dev/kvm` exists for VM runtimes (`kata*`). Throws a single actionable `Error` (`"kernel_runtime '<r>' unavailable: <reason>. See docs/rlm/...-design.md"`) otherwise. `Kernel.start` awaits it before `docker run` when `sandbox === "docker"` and `runtime !== "runc"`.

- [x] **Step 1 (failing test):** in `docker_launcher_test.ts`, assert `preflightRuntime("kata-fc")` rejects with a message containing the runtime name when the runtime is absent (use a fake/missing runtime name so it's deterministic cross-platform). Pure logic — runs on macOS. *(Done: 5 preflight tests via injectable `RuntimeProbe`; verified RED on missing export.)*
- [x] **Step 2:** implement `preflightRuntime`; wire into `Kernel.start`. Keep the existing raw-Docker failure path as a backstop. *(Done: `preflightRuntime` + `RuntimeProbe`/`defaultRuntimeProbe`; awaited in `Kernel.start` before `docker run` for non-`runc`; raw-Docker spawn error retained as backstop.)*
- [x] **Step 3:** verify `buildDockerArgs("kata-fc")` / `buildDockerArgs("kata")` emit `--runtime kata-fc` / `--runtime kata` (extend the existing `runsc` arg test). Run: `export PATH="$HOME/.deno/bin:$PATH" && deno test --allow-read --allow-write --allow-run --allow-env --allow-net tests/docker_launcher_test.ts`. *(Done: 18 tests green, typecheck clean, no regressions.)*

## Task 2: Config surface

**Files:** Modify `src/config.ts`, `rlm_config.yaml`, `fast_rlm/_runner.py`

- [x] **Step 1:** keep `kernel_runtime` a free-form string (so new runtimes need no engine release) but update inline comments + the `config.ts` known-values hint to list `runc | runsc | kata | kata-fc`. *(Done: `config.ts` + `rlm_config.yaml` comments updated.)*
- [x] **Step 2:** ensure `_runner.py` still grants `--allow-run` for `executor: subprocess` (covers the new runtimes — no change expected; add an assertion test if missing). *(Verified: the grant at `_runner.py:353` is keyed on `executor == "subprocess"`, runtime-agnostic, so it already covers `kata`/`kata-fc`. No code change. fast-rlm has no pytest suite by design, so the Deno config test in Step 3 is the test layer.)*
- [x] **Step 3:** add a `config_executor_test.ts` case parsing `kernel_runtime: kata-fc`. *(Done: regression-guard test; 3/3 config tests pass.)*

## Task 3: Hermes passthrough + guard

**Files:** `tools/rlm_tool.py`, `tools/rlm/_driver.py`, `tests/tools/test_rlm_tool.py`, `tests/tools/test_rlm_driver.py`

- [ ] **Step 1 (failing test):** assert `_build_rlm_cfg` forwards `kernel_runtime: kata-fc` verbatim, and that `kernel_sandbox: docker` + a non-local backend still errors (existing guard covers all runtimes).
- [ ] **Step 2:** confirm no code change is needed (the value is opaque to Hermes); fix only if a test reveals filtering.
- [ ] **Step 3:** run `scripts/run_tests.sh tests/tools/test_rlm_tool.py tests/tools/test_rlm_driver.py`.

## Task 4: KVM-gated microVM e2e

**Files:** Modify `third_party/fast-rlm/tests/docker_launcher_test.ts`

- [ ] **Step 1:** add a `kvmAvailable()` gate (Linux + `/dev/kvm` + Kata runtime registered) mirroring the existing `dockerAvailable()` helper; skip with a logged message otherwise (so it's a no-op on macOS/CI without KVM).
- [ ] **Step 2:** when gated in, run the same boot → state → FINAL → `--network none` egress-blocked sequence as the `runc` e2e, but with `runtime: "kata"` (or `kata-fc`). This is the only test that proves the real microVM boundary; document that it requires a Linux/KVM host with Kata installed.

## Task 5: Docs + verification

- [ ] **Step 1:** update `third_party/fast-rlm/CLAUDE.md` to note `kata`/`kata-fc` are recognized `kernel_runtime` values and require a Linux/KVM host with Kata installed (the per-OS matrix from #4 already frames this).
- [ ] **Step 2:** full local gate — `deno test ... tests/docker_launcher_test.ts tests/config_executor_test.ts tests/executor_gate_test.ts` (with `--allow-sys` for the gate test) and `scripts/run_tests.sh tests/tools/test_rlm_tool.py tests/tools/test_rlm_driver.py tests/tools/test_rlm_skill.py`. Record output as evidence (per superpowers:verification-before-completion). The microVM e2e will SKIP locally — note that explicitly.

## Out of scope (later phases)

- `firecracker-containerd` (non-Docker driver) and a native macOS (Seatbelt) / Windows (Hyper-V/AppContainer) executor backend.
- Snapshot/restore warm-start latency optimization.
- Allowlisted egress (still `none` | `bridge`).
- A SANDBOXESCAPEBENCH-style config-hardening CI gate (tracked as a design open question).
