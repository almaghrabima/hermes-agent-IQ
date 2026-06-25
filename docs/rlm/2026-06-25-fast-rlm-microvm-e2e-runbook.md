# Runbook: validate the fast-rlm microVM kernel e2e on a Linux/KVM host

**Date:** 2026-06-25
**Applies to:** `third_party/fast-rlm/tests/docker_launcher_test.ts` →
`microVM kernel e2e: state + FINAL + egress blocked`
**Why this exists:** that test **SKIPs** on macOS/Windows and any host without
`/dev/kvm` + a registered Kata runtime. It is the only test that exercises the real
hardware-VM boundary, so it must be run on a Linux/KVM host to be proven green.
See `2026-06-25-fast-rlm-kernel-phase4-per-os-boundaries-design.md` for the why.

## What the test gates on (must ALL be true, or it skips)

The gate is `kataKvmRuntime()` in `docker_launcher_test.ts`. It returns a runtime
name (and the test runs) only when:

1. `Deno.build.os === "linux"` — a real Linux host (not macOS/Windows, not Docker
   Desktop's VM as seen from a non-Linux host).
2. `/dev/kvm` exists (hardware virtualization available; nested virt counts).
3. `docker info` lists a runtime whose name matches `/kata/i`. A Firecracker-backed
   runtime (name matching `/kata.*(fc|firecracker)/i`, e.g. `kata-fc`) is preferred;
   otherwise the first `kata*` runtime is used.

If any is false → `SKIP: requires a Linux host with /dev/kvm and a Kata runtime registered`.

## Prerequisites

- A **bare-metal or nested-virt-enabled Linux** host (x86_64 or arm64). On a cloud VM,
  ensure nested virtualization is enabled (GCP: a nested-virt-licensed image; AWS:
  `.metal` instance; Azure: a v3+ size that supports nested virt).
- Docker Engine (not just Docker Desktop) with a config you can edit
  (`/etc/docker/daemon.json`).
- `git`, plus **Deno 2+** (`curl -fsSL https://deno.land/install.sh | sh`).
- Root/sudo (to install Kata and edit the Docker daemon config).

## Step 1 — Confirm KVM

```bash
ls -l /dev/kvm                       # must exist
sudo apt-get install -y cpu-checker  # Debian/Ubuntu (optional helper)
kvm-ok                               # "KVM acceleration can be used"
```

No `/dev/kvm`? Enable virtualization in BIOS/cloud settings or pick a nested-virt
host. The test will keep skipping until this exists.

## Step 2 — Install Kata Containers and register it with Docker

Use the official Kata release (kata-deploy or the static tarball — see
https://github.com/kata-containers/kata-containers/blob/main/docs/install/).
Static-tarball sketch:

```bash
# Install kata to /opt/kata and symlink the shims onto PATH (per Kata docs).
# Verify the runtime binary works and sees KVM:
/opt/kata/bin/kata-runtime check         # expect "System is capable of running Kata Containers"
```

Register Kata as a Docker runtime in `/etc/docker/daemon.json`:

```json
{
  "runtimes": {
    "kata": { "path": "/opt/kata/bin/kata-runtime" }
  }
}
```

For the **Firecracker-backed** path the test prefers (`kata-fc`), add a second entry
pointing at a Kata config whose hypervisor is Firecracker (or Cloud Hypervisor). Kata's
Firecracker backend requires `devmapper` snapshotter — follow
https://github.com/kata-containers/kata-containers/blob/main/docs/how-to/how-to-use-virtio-fs-with-firecracker.md
and the Firecracker setup guide. Example once a `configuration-fc.toml` exists:

```json
{
  "runtimes": {
    "kata":    { "path": "/opt/kata/bin/kata-runtime" },
    "kata-fc": { "path": "/opt/kata/bin/kata-runtime",
                 "runtimeArgs": ["--config", "/opt/kata/share/defaults/kata-containers/configuration-fc.toml"] }
  }
}
```

Reload Docker and confirm the runtime is registered:

```bash
sudo systemctl restart docker
docker info --format '{{json .Runtimes}}'   # must include "kata" (and "kata-fc" if configured)
```

## Step 3 — Smoke-test the runtime directly (before the e2e)

```bash
docker run --rm --runtime kata python:3.11-slim python -c "print('kata OK')"
# If you configured it:
docker run --rm --runtime kata-fc python:3.11-slim python -c "print('kata-fc OK')"
```

Both should print `... OK`. A failure here (e.g. `unknown or invalid runtime name`,
or a hypervisor error) means the e2e will also fail — fix Kata first. Note: our
engine's `preflightRuntime` produces the *same class* of clean error if you point
Hermes at an unregistered runtime, so this is also a good check of that path.

## Step 4 — Run the gated microVM e2e

```bash
git clone <this repo> && cd hermes-agent-IQ/third_party/fast-rlm   # or use your checkout
export PATH="$HOME/.deno/bin:$PATH"

deno test --allow-read --allow-write --allow-run --allow-env --allow-net --allow-sys \
  tests/docker_launcher_test.ts
```

**Expected on a correctly-configured host:** the line

```
microVM kernel e2e: state + FINAL + egress blocked (skips without Linux/KVM/Kata) ... ok
```

with **no** `SKIP:` message, and `13 passed | 0 failed` (12 that already pass on macOS
+ this one now running). The e2e asserts, inside the microVM:
- persistent state across steps (`x = 41` → `x += 1` → prints `42`),
- `--network none` blocks egress (`urllib.urlopen` → `NET_BLOCKED`),
- the stdio control channel still works (`__js_llm_query__('hi')` → `hi`),
- `FINAL({'v': x})` resolves to `{ v: 42 }`.

If you only configured plain `kata` (not `kata-fc`), the test auto-selects `kata` —
that still validates the microVM boundary; it just isn't the Firecracker hypervisor.

## Step 5 — (optional) End-to-end through Hermes

To exercise the full Hermes → driver → engine path with a real model:

```yaml
# ~/.hermes/config.yaml  (resolve via get_hermes_home(); never hardcode ~/.hermes)
rlm:
  engine_path: /abs/path/to/kernel-capable/fast-rlm   # kernel support required (fork build)
  executor: subprocess
  kernel_sandbox: docker
  kernel_runtime: kata-fc        # or "kata"
  kernel_image: python:3.11-slim
  kernel_network: none
```

Then invoke the `rlm` tool normally. The docker-kernel guard requires the **local**
Hermes backend (not modal/daytona). Note the existing key-staging caveat in
`skills/recursive-language-model/SKILL.md`.

## Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| Test still prints `SKIP: requires a Linux host...` | One of the three gate conditions is false. Re-check `uname` (Linux), `ls /dev/kvm`, and `docker info --format '{{json .Runtimes}}'` (must contain `kata`). |
| `unknown or invalid runtime name: kata` | Daemon not reloaded, or `daemon.json` typo. `sudo systemctl restart docker`, re-run `docker info`. |
| `kata-runtime check` fails on KVM | No `/dev/kvm` / nested virt disabled. Fix at the BIOS/cloud layer. |
| Hypervisor/devmapper errors with `kata-fc` | Firecracker backend needs the `devmapper` snapshotter and a FC-specific Kata config. Fall back to plain `kata` to confirm the boundary, then revisit FC setup. |
| Egress assertion fails (`NET_OK` seen) | The container has network. Confirm `kernel_network: none` / the test passes `network: "none"` (it does) and that no Docker default network override is forcing connectivity. |
| `--allow-sys` permission error in Deno | Include `--allow-sys` (the gate/oidc path reads `hostname`). |

## Reporting back

When green on the Linux/KVM host, record it the same way the plan's macOS evidence
was recorded — paste the `deno test` summary line (expect `13 passed | 0 failed`, no
`SKIP`) into the Phase 4 plan's verification-evidence section and note the host
(distro, arch, Kata version, hypervisor: runc-vs-FC). That flips the one remaining
unverified item in
`2026-06-25-fast-rlm-kernel-phase4-per-os-boundaries-plan.md` to confirmed.
