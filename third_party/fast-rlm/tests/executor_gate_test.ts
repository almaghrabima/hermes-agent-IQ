import { assert } from "jsr:@std/assert@^1.0.0";

// The unsandboxed gate must be its own exported, pure function so it is testable
// without spinning the whole engine.
import { assertSubprocessAllowed } from "../src/subagents.ts";

Deno.test("subprocess + local requires ack", () => {
  let threw = false;
  try { assertSubprocessAllowed({ executor: "subprocess", kernel_sandbox: "local", executor_unsandboxed_ack: false }); }
  catch (e) { threw = true; assert(String((e as Error).message).includes("executor_unsandboxed_ack")); }
  assert(threw);
});

Deno.test("subprocess + local with ack allowed", () => {
  assertSubprocessAllowed({ executor: "subprocess", kernel_sandbox: "local", executor_unsandboxed_ack: true });
});

Deno.test("subprocess + docker needs NO ack", () => {
  assertSubprocessAllowed({ executor: "subprocess", kernel_sandbox: "docker", executor_unsandboxed_ack: false });
});

Deno.test("pyodide needs no ack", () => {
  assertSubprocessAllowed({ executor: "pyodide" });
});
