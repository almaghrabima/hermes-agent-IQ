import { assert } from "jsr:@std/assert@^1.0.0";

// The unsandboxed gate must be its own exported, pure function so it is testable
// without spinning the whole engine.
import { assertSubprocessAllowed } from "../src/subagents.ts";

Deno.test("subprocess executor refuses without ack", () => {
  let threw = false;
  try {
    assertSubprocessAllowed({ executor: "subprocess", executor_unsandboxed_ack: false });
  } catch (e) {
    threw = true;
    assert(String((e as Error).message).includes("executor_unsandboxed_ack"));
  }
  assert(threw, "expected refusal without ack");
});

Deno.test("subprocess executor allowed with ack", () => {
  assertSubprocessAllowed({ executor: "subprocess", executor_unsandboxed_ack: true });
});

Deno.test("pyodide executor needs no ack", () => {
  assertSubprocessAllowed({ executor: "pyodide" });
});
