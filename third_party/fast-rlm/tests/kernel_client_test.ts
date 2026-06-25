import { assert, assertEquals } from "jsr:@std/assert@^1.0.0";
import { Kernel } from "../src/kernel_client.ts";

const KERNEL = new URL("../python_kernel/kernel.py", import.meta.url).pathname;

Deno.test("kernel: state persistence + FINAL", async () => {
  const k = await Kernel.start({ python: "python3", kernelPath: KERNEL, handlers: {} });
  await k.setup(
    "def FINAL(x):\n globals()['__final_result__']=x\n globals()['__final_result_set__']=True\n",
  );
  const r1 = await k.runStep("x = 41\nprint('boot')");
  assert(r1.stdout.includes("boot"));
  assertEquals(r1.final_set, false);
  const r2 = await k.runStep("x += 1\nprint(x)");
  assert(r2.stdout.includes("42"));
  const r3 = await k.runStep("FINAL({'v': x})");
  assertEquals(r3.final_set, true);
  assertEquals(r3.final_value, { v: 42 });
  await k.shutdown();
});

Deno.test("kernel: llm_query callback routes to host handler", async () => {
  let seen: unknown = null;
  const handlers = {
    llm_query: (p: { context: unknown }) => {
      seen = p.context;
      return Promise.resolve({ doubled: 84 });
    },
  };
  const k = await Kernel.start({ python: "python3", kernelPath: KERNEL, handlers });
  await k.setup("pass\n");
  const r = await k.runStep("res = await __js_llm_query__('ctx')\nprint(res['doubled'])");
  assert(r.stdout.includes("84"));
  assertEquals(seen, "ctx");
  await k.shutdown();
});

Deno.test("kernel: batch concurrency (two parallel llm_query via gather)", async () => {
  const handlers = {
    llm_query: async (p: { context: number }) => {
      await new Promise((r) => setTimeout(r, 10));
      return { n: p.context };
    },
  };
  const k = await Kernel.start({ python: "python3", kernelPath: KERNEL, handlers });
  await k.setup("import asyncio\n");
  const r = await k.runStep(
    "import asyncio\n" +
      "a, b = await asyncio.gather(__js_llm_query__(1), __js_llm_query__(2))\n" +
      "print(a['n'] + b['n'])",
  );
  assert(r.stdout.includes("3"));
  await k.shutdown();
});
