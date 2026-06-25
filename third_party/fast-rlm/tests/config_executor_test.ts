import { assertEquals } from "jsr:@std/assert@^1.0.0";
import { parse as parseYaml } from "jsr:@std/yaml@^1.0.0";
import type { RlmConfig } from "../src/config.ts";

Deno.test("config parses executor keys", () => {
  const cfg = parseYaml(
    "executor: subprocess\nexecutor_unsandboxed_ack: true\n",
  ) as RlmConfig;
  assertEquals(cfg.executor, "subprocess");
  assertEquals(cfg.executor_unsandboxed_ack, true);
});
