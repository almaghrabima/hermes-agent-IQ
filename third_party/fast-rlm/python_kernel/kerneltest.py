"""Stdlib test driver for kernel.py (run: python3 kerneltest.py). Exit 0 = pass."""
import asyncio
import os
import socket
import sys

sys.path.insert(0, os.path.dirname(__file__))
import kernel as K  # noqa: E402

FAILURES = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        FAILURES.append(name)


async def _pair():
    a, b = socket.socketpair()
    ra, wa = await asyncio.open_connection(sock=a)
    rb, wb = await asyncio.open_connection(sock=b)
    return (ra, wa), (rb, wb)


async def _setup(k, code):
    # exec setup directly into G (mirrors the 'setup' op without the socket)
    exec(compile(code, "<setup>", "exec"), k.G)


_FINAL_DEF = (
    "def FINAL(x):\n"
    " globals()['__final_result__']=x\n"
    " globals()['__final_result_set__']=True\n"
)


async def test_state_and_final():
    (ra, wa), _ = await _pair()
    k = K.Kernel(ra, wa)
    k._inject_bridge()
    await _setup(k, _FINAL_DEF)
    r1 = await k.run_step("x = 40\nprint('hi')")
    check("step1 stdout", "hi" in r1["stdout"])
    check("step1 no final", r1["final_set"] is False)
    r2 = await k.run_step("x += 2\nprint(x)")
    check("state persists", "42" in r2["stdout"])
    r3 = await k.run_step("FINAL({'answer': x})")
    check("final set", r3["final_set"] is True)
    check("final value", r3["final_value"] == {"answer": 42})


async def test_register_tool_and_error():
    (ra, wa), _ = await _pair()
    k = K.Kernel(ra, wa)
    k._inject_bridge()
    await _setup(k, _FINAL_DEF)
    k._register_tool("def add(a, b):\n    return a + b\n")
    r = await k.run_step("print(add(2, 3))")
    check("tool registered", "5" in r["stdout"])
    rerr = await k.run_step("raise ValueError('boom')")
    check("exception captured in stdout", "boom" in rerr["stdout"])
    check("exception in error field", "ValueError" in rerr["error"])


async def main():
    await test_state_and_final()
    await test_register_tool_and_error()
    print(("FAILED: " + ", ".join(FAILURES)) if FAILURES else "ALL PASS")
    sys.exit(1 if FAILURES else 0)


if __name__ == "__main__":
    asyncio.run(main())
