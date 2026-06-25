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

Deno.test("config parses kernel sandbox keys", () => {
  const cfg = parseYaml(
    "kernel_sandbox: docker\nkernel_runtime: runsc\nkernel_image: python:3.11-slim\nkernel_network: none\n",
  ) as RlmConfig;
  assertEquals(cfg.kernel_sandbox, "docker");
  assertEquals(cfg.kernel_runtime, "runsc");
  assertEquals(cfg.kernel_image, "python:3.11-slim");
  assertEquals(cfg.kernel_network, "none");
});
