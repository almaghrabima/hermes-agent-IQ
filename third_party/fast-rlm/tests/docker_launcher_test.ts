import { assert, assertEquals } from "jsr:@std/assert@^1.0.0";
import { buildDockerArgs, Kernel } from "../src/kernel_client.ts";

Deno.test("buildDockerArgs: runc omits --runtime, mounts kernel ro, network none", () => {
  const args = buildDockerArgs({
    kernelPath: "/abs/python_kernel/kernel.py",
    image: "python:3.11-slim", runtime: "runc", network: "none", name: "rlm-kernel-x",
  });
  const joined = args.join(" ");
  assertEquals(args[0], "run");
  assert(joined.includes("--rm -i"));
  assert(!joined.includes("--runtime"), "runc must NOT pass --runtime");
  assert(joined.includes("--network none"));
  assert(joined.includes("-v /abs/python_kernel/kernel.py:/kernel.py:ro"));
  assert(joined.includes("python:3.11-slim"));
  assert(joined.endsWith("python /kernel.py --stdio"));
});

Deno.test("buildDockerArgs: runsc passes --runtime runsc", () => {
  const args = buildDockerArgs({
    kernelPath: "/k.py", image: "img", runtime: "runsc", network: "bridge", name: "n",
  });
  assert(args.join(" ").includes("--runtime runsc"));
  assert(args.join(" ").includes("--network bridge"));
});

// Gated e2e: only runs when Docker is available.
async function dockerAvailable(): Promise<boolean> {
  try {
    const p = new Deno.Command("docker", { args: ["info"], stdout: "null", stderr: "null" }).spawn();
    return (await p.status).success;
  } catch { return false; }
}

Deno.test("docker kernel e2e: state + FINAL over stdio (skips if no docker)", async () => {
  if (!await dockerAvailable()) { console.log("SKIP: docker unavailable"); return; }
  const KERNEL = new URL("../python_kernel/kernel.py", import.meta.url).pathname;
  const k = await Kernel.start({
    python: "python3", kernelPath: KERNEL, handlers: {},
    sandbox: "docker", runtime: "runc", image: "python:3.11-slim", network: "none",
  });
  await k.setup("def FINAL(x):\n globals()['__final_result__']=x\n globals()['__final_result_set__']=True\n");
  const r1 = await k.runStep("x = 41\nprint('boot')");
  assert(r1.stdout.includes("boot"));
  const r2 = await k.runStep("x += 1\nprint(x)");
  assert(r2.stdout.includes("42"));
  const r3 = await k.runStep("FINAL({'v': x})");
  assertEquals(r3.final_set, true);
  assertEquals(r3.final_value, { v: 42 });
  await k.shutdown();
});

Deno.test("docker --network none blocks agent egress; control still works (skips if no docker)", async () => {
  if (!await dockerAvailable()) { console.log("SKIP: docker unavailable"); return; }
  const KERNEL = new URL("../python_kernel/kernel.py", import.meta.url).pathname;
  const k = await Kernel.start({
    python: "python3", kernelPath: KERNEL,
    handlers: { llm_query: (p: { context: unknown }) => Promise.resolve({ echo: p.context }) },
    sandbox: "docker", runtime: "runc", image: "python:3.11-slim", network: "none",
  });
  await k.setup("pass\n");
  // egress blocked:
  const r = await k.runStep(
    "import urllib.request\n" +
      "try:\n urllib.request.urlopen('http://example.com', timeout=3); print('NET_OK')\n" +
      "except Exception as e:\n print('NET_BLOCKED')\n",
  );
  assert(r.stdout.includes("NET_BLOCKED"));
  // control channel still works:
  const r2 = await k.runStep("res = await __js_llm_query__('hi')\nprint(res['echo'])");
  assert(r2.stdout.includes("hi"));
  await k.shutdown();
});

Deno.test("docker bad image: setup rejects (no hang) (skips if no docker)", async () => {
  if (!await dockerAvailable()) { console.log("SKIP: docker unavailable"); return; }
  const KERNEL = new URL("../python_kernel/kernel.py", import.meta.url).pathname;
  const k = await Kernel.start({
    python: "python3", kernelPath: KERNEL, handlers: {},
    sandbox: "docker", runtime: "runc", image: "nonexistent/rlm-bad-image-xyz:nope", network: "none",
  });
  let rejected = false;
  try {
    await Promise.race([
      k.setup("pass\n"),
      new Promise((_, rej) => setTimeout(() => rej(new Error("TIMEOUT: setup hung")), 15000)),
    ]);
  } catch (e) {
    rejected = true;
    assert(!String((e as Error).message).includes("TIMEOUT"), "setup hung instead of rejecting");
  } finally { k.close(); }
  assert(rejected, "expected setup to reject on a bad image");
});
