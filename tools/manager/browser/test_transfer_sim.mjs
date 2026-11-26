// Driven by tools/tests/test_js_transfer.py. Runs the JS transfer client against the C++ device simulator
// (tools/tests/transfer_device_sim.cc: the real NativeStage on simulated NOR behind the real FirmwarePort),
// optionally over a lossy link, and prints one JSON line with the simulator's verdict.
//
//   node test_transfer_sim.mjs SIM BEFORE.bin PLAN.bin KIND UPDATE.json [loss corrupt duplicate reorder seed]
import {spawn} from "node:child_process";
import {createHash} from "node:crypto";
import fs from "node:fs";
import {upload, records} from "./transfer_client.js";

const [simPath, before, planPath, kind, updatePath, ...rest] = process.argv.slice(2);
const [loss = 0, corrupt = 0, duplicate = 0, reorder = 0, seed = 1] = rest.map(Number);
const faulty = loss || corrupt || duplicate || reorder;

function mulberry32(a) {
  return () => { a = (a + 0x6d2b79f5) | 0; let t = Math.imul(a ^ (a >>> 15), 1 | a); t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t; return ((t ^ (t >>> 14)) >>> 0) / 4294967296; };
}
const rng = mulberry32(seed);

class Sim {
  constructor() {
    this.proc = spawn(simPath, [before, planPath, kind], {stdio: ["pipe", "pipe", "inherit"]});
    this.buffer = Buffer.alloc(0);
    this.waiters = [];
    this.proc.stdout.on("data", (chunk) => { this.buffer = Buffer.concat([this.buffer, chunk]); this.pump(); });
    this.replies = [];
  }
  pump() {
    while (this.waiters.length && this.waiters[0].ready(this.buffer)) {
      const waiter = this.waiters.shift();
      const taken = waiter.take(this.buffer);
      this.buffer = this.buffer.subarray(taken.used);
      waiter.resolve(taken.value);
    }
  }
  wait(ready, take) { return new Promise((resolve) => { this.waiters.push({ready, take, resolve}); this.pump(); }); }
  bytes(n) { return this.wait((b) => b.length >= n, (b) => ({used: n, value: b.subarray(0, n)})); }
  line() { return this.wait((b) => b.includes(10), (b) => { const i = b.indexOf(10); return {used: i + 1, value: b.subarray(0, i).toString()}; }); }
  damage(report) {
    const out = Buffer.from(report);
    if (rng() < corrupt) out[Math.floor(rng() * out.length)] ^= 1 << Math.floor(rng() * 8);
    return out;
  }
  async write(report) {
    const copies = rng() < duplicate ? 2 : 1;
    for (let c = 0; c < copies; c++) {
      if (rng() < loss) continue;
      this.proc.stdin.write(Buffer.concat([Buffer.from("R"), this.damage(report)]));
      const tag = (await this.bytes(1))[0];
      if (tag === 0x52) { // 'R'
        const reply = Buffer.from(await this.bytes(63));
        if (rng() >= loss) {
          this.replies.push(this.damage(reply));
          if (rng() < duplicate) this.replies.push(this.replies.at(-1));
        }
      }
    }
  }
  async read(timeoutMs) {
    if (this.replies.length) {
      const index = this.replies.length > 1 && rng() < reorder ? 1 : 0;
      return new Uint8Array(this.replies.splice(index, 1)[0]);
    }
    await new Promise((resolve) => setTimeout(resolve, Math.min(timeoutMs, 0.5)));
    return null;
  }
  async finish() {
    this.proc.stdin.write("E");
    const verdict = await this.line();
    this.proc.stdin.end();
    return verdict.trim();
  }
  kill() { this.proc.kill(); }
}

const spec = JSON.parse(fs.readFileSync(updatePath, "utf8"));
const update = {
  segments: spec.segments.map(([segment, hex]) => [segment, Uint8Array.from(Buffer.from(hex, "hex"))]),
  metadata: spec.metadata,
  payload: Uint8Array.from(Buffer.from(spec.payload, "hex")),
};
const sim = new Sim();
const console_ = {
  async command(text) {
    if (text.includes("transfer-capabilities")) return "OK XFER1 49";
    if (text.includes("transfer-kind")) return `OK ${kind}`;
    return "OK";
  },
};
const messages = [];
try {
  await upload(console_, sim, update, (text) => messages.push(text), faulty ? {stallMs: 20, retries: 200} : {});
  const verdict = await sim.finish();
  console.log(JSON.stringify({verdict, messages, recordsSha256: createHash("sha256").update(records(update)).digest("hex")}));
} catch (error) {
  sim.kill();
  console.log(JSON.stringify({error: String(error.message || error), messages}));
  process.exitCode = 1;
}
