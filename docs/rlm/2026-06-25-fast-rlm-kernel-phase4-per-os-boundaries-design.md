# Design: fast-rlm kernel sandboxing (Phase 4 — per-OS isolation boundaries + microVM backend)

**Date:** 2026-06-25
**Status:** Proposed (design)
**Part of:** "Replace Pyodide, route execution through Hermes backends" effort.
**Builds on:** Phase 2 (containerized kernel: `runc` default, `runsc`/gVisor opt-in) and
Phase 3 (Hermes `rlm` tool → fast-rlm sandbox passthrough, merged).

## Context

Phases 1–3 gave the `subprocess` executor a containerized kernel with a selectable
Docker runtime (`runc` / `runsc`) driven over a stdio control channel, with
`--network none` by default. The open question that motivated this phase: **gVisor
(`runsc`) is Linux-only**, so "what is the strongest practical boundary on each of
macOS / Windows / Linux?" was unresolved, and the `docker` sandbox mode silently
relies on the host's Linux VM on non-Linux hosts.

This doc records the conclusions of a verified multi-source research pass
(2026-06-25; 24 adversarially-verified claims, 1 refuted) and proposes (a) a per-OS
boundary matrix to document and (b) an optional Firecracker/Kata microVM backend
for the strongest Linux isolation.

### The constraint that drives scope

**True VM-level isolation (Firecracker / Kata / gVisor-KVM-platform) requires a Linux
host with `/dev/kvm`.** It is *not* natively available on macOS or Windows. So the
recommended boundary genuinely differs per OS, and our Docker-based path only reaches
its full strength on Linux.

### Architectural tailwind (already in place)

Because `llm_query`/MCP calls ride the **stdio control channel to the host** (not the
container network), **every** option below can run with network egress fully denied by
default while agent recursion keeps working. The kernel needs only stdio + a
read-only `kernel.py` mount — no inbound or outbound network. This narrows the viable
options considerably and is a deliberate strength to preserve.

## Verified findings (per-OS recommendation)

| OS | Primary (strongest practical) | Fallback (low-friction) |
|----|-------------------------------|-------------------------|
| **Linux** | **Firecracker microVM** — hardware KVM boundary, own kernel per VM, ~50k-LOC Rust attack surface, ≤125ms boot-to-init, <5 MiB VMM overhead, full native CPython. Basis of AWS Lambda, E2B, Vercel Sandbox. | **gVisor (`runsc`)** — current path. User-space syscall interception (host syscalls ~400→~50), ~100ms start, OCI drop-in, **not** hardware-level, 10–30% I/O tax. *Kata Containers* if running under Kubernetes and VM-grade isolation is wanted inside the OCI model. |
| **macOS** | **Linux microVM / gVisor hosted inside a host VM** (Docker Desktop's Linux VM, or a dedicated Virtualization.framework / Lima VM) — i.e. the *Linux* boundary, hosted. Only strong boundary on Mac. | **Seatbelt via `sandbox-exec`** — native, low-latency, fine-grained file/syscall rules, but a **repeatedly/architecturally bypassed** boundary (10+ recent escape CVEs; "drop an unquarantined file = full escape"). Trusted-input only. |
| **Windows** | **Hyper-V isolation / Hyper-V-isolated containers** — true security boundary, own kernel per container. *Process-isolation containers are explicitly NOT a security boundary (Microsoft). Windows Sandbox is strong but ephemeral — discards state, conflicting with a persistent REPL.* | **WSL2 running the Linux `runc`/`runsc` stack** (Cursor's pragmatic choice; they judged native Windows primitives insufficient for general tools). One isolation codepath. *Win32 App Isolation (AppContainer) is a good native default-deny-network primitive but same-kernel + needs MSIX packaging.* |

### Cross-cutting evidence

- **Bare `runc`/OCI is not automatically sufficient** against capable agents. ICML 2026
  (Oxford + UK AI Security Institute) found frontier LLMs *can* identify and exploit
  container-escape vulns **when the config is misconfigured/vulnerable** — it does
  *not* claim they break a properly-configured Docker. Justifies the Phase 2 move
  beyond bare `runc` to `runsc`/VM, plus a hardening checklist.
- **WASM (Pyodide) had zero supporting evidence** as a top-tier boundary: a
  language-level boundary that breaks native CPython / C-extensions (no pandas/numpy).
  This matches our existing split — Pyodide is the *safe-but-limited* default;
  `subprocess`+sandbox is the *full-Python* path.
- **Millisecond/MB figures are best-case.** Firecracker's 125ms is boot-to-init, not
  REPL-ready; Kata's ~30MB/~200ms assume a lightweight hypervisor (default QEMU is
  ~3× heavier). Re-benchmark for a persistent kernel.

## Goals

- **Document** the per-OS boundary matrix in the skill (`recursive-language-model/SKILL.md`)
  and `third_party/fast-rlm/CLAUDE.md`, including the "no strong *native* boundary on
  macOS/Windows; `docker` mode relies on the host Linux VM there" caveat.
- **Add an optional `firecracker` (and/or `kata`) kernel runtime** on Linux/KVM hosts,
  reusing the existing `kernel_runtime` config knob and the stdio control channel.
- Preserve **default-deny network egress** for the new backend (no virtio-net device).
- Keep `runc`/`runsc` and `local` paths unchanged; the microVM backend is opt-in and
  Linux/KVM-gated, failing cleanly (like `runsc` does today) when `/dev/kvm` is absent.
- Provide a **hardening checklist** gate for the kernel executor config.

## Non-goals (Phase 4)

- No native macOS (Seatbelt) or Windows (AppContainer/Hyper-V) executor backend — the
  recommendation on those OSes is "host a Linux VM" (Docker Desktop / WSL2), which we
  already lean on. Native backends are a possible later phase, not this one.
- No microVM image-build pipeline beyond bind-mounting `kernel.py` into a stock rootfs.
- No allowlisted-egress (still `none` or `bridge`); selective egress is a separate item.
- No snapshot/restore warm-start optimization (tracked as an open question).

## Config surface (proposed)

Reuse `kernel_runtime`; extend its accepted values. `docker` sandbox mode dispatches
on runtime as today:

| `kernel_runtime` | Boundary | Host requirement |
|---|---|---|
| `runc` *(default)* | namespaces + cgroups | Docker |
| `runsc` | gVisor user-space kernel | Linux + gVisor |
| `firecracker` *(new)* | KVM microVM, own kernel | Linux + `/dev/kvm` |
| `kata` *(new, optional)* | KVM per-container VM, OCI model | Linux + `/dev/kvm` |

Selecting `firecracker`/`kata` on a host without `/dev/kvm` must fail with a clear
error (mirroring Docker's "unknown runtime" behavior we verified for `runsc` on macOS),
never silently fall back to a weaker boundary.

## Testing strategy

- **Cross-platform unit:** runtime→launch-args construction for `firecracker`/`kata`
  (analogous to the existing `buildDockerArgs: runsc passes --runtime runsc` test) —
  validates wiring without needing KVM, so it runs on the macOS dev host.
- **Linux/KVM e2e (gated):** kernel boot + state + FINAL over stdio inside a microVM;
  `--network`-denied egress blocked while the stdio control channel still works
  (mirrors the existing `docker --network none blocks egress` test).
- **Clean-failure test:** `firecracker` requested on a non-KVM host rejects, no hang.
- **Python side:** `rlm_tool` config threading + backend guard tests for the new values.

## Open questions

- Real **Python-REPL-ready warm/restore latency** (not boot-to-init) for Firecracker
  snapshots vs gVisor vs a persistent Kata pod, for a long-lived kernel.
- Is **WASM** viable for a subset of agent Python where C-extensions aren't needed?
- On macOS, can per-execution gVisor/microVM nesting be layered inside Docker Desktop's
  shared Linux VM, or is a dedicated Virtualization.framework/Lima VM per kernel better?
- Per-boundary **default-deny egress with selective allowlisting** that survives a
  persistent session.
- A **SANDBOXESCAPEBENCH-style** continuous config-hardening gate before deployment.

## Sources (verified)

- Firecracker `SPECIFICATION.md` — github.com/firecracker-microvm/firecracker (primary)
- gVisor security model — gvisor.dev/docs/architecture_guide/security/ (primary)
- Kata + Agent Sandbox — katacontainers.io; kubernetes.io/blog/2026/03/20/;
  agent-sandbox.sigs.k8s.io
- Cursor agent sandboxing (Seatbelt/Landlock/WSL2 practitioner choices) — cursor.com/blog/agent-sandboxing
- macOS sandbox escapes — jhftss.github.io/A-New-Era-of-macOS-Sandbox-Escapes/;
  projectzero.google/2022/03/forcedentry-sandbox-escape.html
- Windows app isolation / Hyper-V — learn.microsoft.com (application-isolation,
  hyperv-container); blogs.windows.com (Win32 App Isolation for Python, 2024)
- "Quantifying Frontier LLM Capabilities for Container Sandbox Escape" — arxiv.org/pdf/2603.02277;
  github.com/UKGovernmentBEIS/sandbox_escape_bench (ICML 2026)
