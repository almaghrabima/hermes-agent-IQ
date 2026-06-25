// Deno client for the out-of-process fast-rlm Python kernel (Phase 1).
// Spawns kernel.py, connects over a UNIX socket (POSIX) / TCP loopback (Windows),
// and multiplexes duplex length-prefixed JSON frames.

type Frame = { kind: "req" | "resp"; op?: string; id: number; [k: string]: unknown };
// deno-lint-ignore no-explicit-any
type HostHandler = (payload: any) => Promise<unknown>;

export interface KernelStartOpts {
  python: string;
  kernelPath: string;
  handlers: Record<string, HostHandler>;
}

export interface Transport {
  readChunk(): Promise<Uint8Array | null>;
  write(bytes: Uint8Array): Promise<void>;
  close(): void;
}

/** Transport over a Deno.Conn (the Phase-1 local unix-socket path). */
class ConnTransport implements Transport {
  #conn: Deno.Conn;
  #listener?: Deno.Listener;
  #socketPath?: string;
  #proc?: Deno.ChildProcess;
  #chunk = new Uint8Array(65536);

  constructor(conn: Deno.Conn, opts: { listener?: Deno.Listener; socketPath?: string; proc?: Deno.ChildProcess }) {
    this.#conn = conn;
    this.#listener = opts.listener;
    this.#socketPath = opts.socketPath;
    this.#proc = opts.proc;
  }

  async readChunk(): Promise<Uint8Array | null> {
    const n = await this.#conn.read(this.#chunk);
    return n === null ? null : this.#chunk.subarray(0, n);
  }

  async write(bytes: Uint8Array): Promise<void> {
    let off = 0;
    while (off < bytes.length) {
      const n = await this.#conn.write(bytes.subarray(off));
      if (n <= 0) throw new Error("kernel socket write returned " + n);
      off += n;
    }
  }

  close(): void {
    try { this.#conn.close(); } catch { /* ignore */ }
    try { this.#listener?.close(); } catch { /* ignore */ }
    try { this.#proc?.kill(); } catch { /* ignore */ }
    if (this.#socketPath) {
      try { Deno.removeSync(this.#socketPath); } catch { /* ignore */ }
    }
  }
}

export interface StepResult {
  stdout: string;
  error: string;
  final_set: boolean;
  final_value: unknown;
  final_error: string | null;
}

function pack(obj: unknown): Uint8Array {
  const body = new TextEncoder().encode(JSON.stringify(obj));
  const out = new Uint8Array(4 + body.length);
  new DataView(out.buffer).setUint32(0, body.length, false); // big-endian
  out.set(body, 4);
  return out;
}

export class Kernel {
  #transport: Transport;
  #handlers: Record<string, HostHandler>;
  #pending = new Map<number, { resolve: (v: unknown) => void; reject: (e: Error) => void }>();
  #nextId = 2; // host owns EVEN ids
  #buf = new Uint8Array(0);
  #closed = false;
  #writeChain: Promise<void> = Promise.resolve();

  private constructor(transport: Transport, handlers: Record<string, HostHandler>) {
    this.#transport = transport;
    this.#handlers = handlers;
    this.#readLoop();
  }

  static async start(opts: KernelStartOpts): Promise<Kernel> {
    const socketPath = `${Deno.makeTempDirSync()}/rlm-kernel.sock`;
    const listener = Deno.listen({ transport: "unix", path: socketPath });
    const proc = new Deno.Command(opts.python, {
      args: [opts.kernelPath, "--socket", socketPath],
      stdout: "inherit", stderr: "inherit",
    }).spawn();
    const conn = await listener.accept();
    const transport = new ConnTransport(conn, { listener, socketPath, proc });
    return new Kernel(transport, opts.handlers);
  }

  #send(frame: Frame): Promise<void> {
    const bytes = pack(frame);
    const task = this.#writeChain.then(() => this.#transport.write(bytes));
    this.#writeChain = task.catch(() => {});
    return task;
  }

  #request(op: string, extra: Record<string, unknown>): Promise<unknown> {
    const id = this.#nextId;
    this.#nextId += 2; // even ids
    return new Promise((resolve, reject) => {
      this.#pending.set(id, { resolve, reject });
      this.#send({ kind: "req", op, id, ...extra }).catch(reject);
    });
  }

  async #readLoop(): Promise<void> {
    while (!this.#closed) {
      let chunk: Uint8Array | null;
      try { chunk = await this.#transport.readChunk(); } catch { break; }
      if (chunk === null) break;
      const merged = new Uint8Array(this.#buf.length + chunk.length);
      merged.set(this.#buf);
      merged.set(chunk, this.#buf.length);
      this.#buf = merged;
      this.#drainFrames();
    }
  }

  #drainFrames(): void {
    while (this.#buf.length >= 4) {
      const len = new DataView(this.#buf.buffer, this.#buf.byteOffset, 4).getUint32(0, false);
      if (this.#buf.length < 4 + len) break;
      const body = this.#buf.subarray(4, 4 + len);
      const frame = JSON.parse(new TextDecoder().decode(body)) as Frame;
      this.#buf = this.#buf.subarray(4 + len);
      this.#dispatch(frame);
    }
  }

  #dispatch(frame: Frame): void {
    if (frame.kind === "resp") {
      const p = this.#pending.get(frame.id);
      if (p) {
        this.#pending.delete(frame.id);
        // The `error` field is overloaded: for setup/register_tool/reset_final
        // it signals request failure, but for run_step it carries the agent's
        // captured Python traceback as DATA. So the generic handler NEVER
        // rejects on `error` — each typed method interprets it. (Transport
        // failures are handled by the read loop ending / close().)
        p.resolve(frame); // whole frame (run_step fields) or {result}
      }
      return;
    }
    // kernel -> host request
    const handler = this.#handlers[frame.op as string];
    const reply = (extra: Record<string, unknown>) =>
      this.#send({ kind: "resp", id: frame.id, ...extra }).catch(() => {});
    if (!handler) {
      reply({ error: `no host handler for ${frame.op}` });
      return;
    }
    Promise.resolve(handler(frame as Record<string, unknown>))
      .then((result) => reply({ result }))
      .catch((e) => reply({ error: String(e?.message ?? e) }));
  }

  async setup(code: string): Promise<void> {
    const f = (await this.#request("setup", { code })) as Frame;
    if (f.error) throw new Error(String(f.error));
  }
  async registerTool(src: string): Promise<void> {
    const f = (await this.#request("register_tool", { src })) as Frame;
    if (f.error) throw new Error(String(f.error));
  }
  async runStep(code: string): Promise<StepResult> {
    // NEVER throws: a non-empty `error` is the agent's captured traceback,
    // which is normal RLM flow and returned to the caller as data.
    const f = (await this.#request("run_step", { code })) as Frame;
    return {
      stdout: String(f.stdout ?? ""),
      error: String(f.error ?? ""),
      final_set: Boolean(f.final_set),
      final_value: f.final_value,
      final_error: (f.final_error as string | null) ?? null,
    };
  }
  async resetFinal(): Promise<void> {
    const f = (await this.#request("reset_final", {})) as Frame;
    if (f.error) throw new Error(String(f.error));
  }
  async shutdown(): Promise<void> {
    try {
      await this.#request("shutdown", {});
    } catch {
      // connection may drop as the kernel exits
    }
    this.close();
  }
  close(): void {
    if (this.#closed) return;
    this.#closed = true;
    for (const [, p] of this.#pending) p.reject(new Error("kernel connection closed"));
    this.#pending.clear();
    this.#transport.close();
  }
}
