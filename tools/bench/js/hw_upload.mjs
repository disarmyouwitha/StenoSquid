// Lab tool, run by tools/bench/run_js_upload_hw.py: runs the REAL browser transfer client (transfer_client.js)
// against a real keyboard. Node has no HID, so Python relays reports over this process's stdio:
//   node -> python:  'W' + 63 bytes          write one report (no reply)
//                    'R' + u16 timeout ms    read one report   -> 'R' + 63 bytes | 'N'
//                    'C' + u16 n + text      console command   -> 'C' + u16 n + reply
//                    'D' + u32 n + JSON      finished (stats, messages or error)
import fs from "node:fs";
import {upload} from "../../manager/browser/transfer_client.js";

const updatePath = process.argv[2];
let buffer = Buffer.alloc(0);
const waiters = [];
process.stdin.on("data", (chunk) => { buffer = Buffer.concat([buffer, chunk]); pump(); });
function pump() {
  while (waiters.length && buffer.length >= waiters[0].n) {
    const {n, resolve} = waiters.shift();
    const taken = buffer.subarray(0, n);
    buffer = buffer.subarray(n);
    resolve(taken);
  }
}
const expect = (n) => new Promise((resolve) => { waiters.push({n, resolve}); pump(); });
const u16 = (value) => { const b = Buffer.alloc(2); b.writeUInt16LE(value); return b; };
const send = (...parts) => process.stdout.write(Buffer.concat(parts.map((p) => (typeof p === "string" ? Buffer.from(p) : p))));

// Loss injection for the lost-ACK hardware test: STENO_DROP_WRITES / STENO_DROP_READS are probabilities (0..1) of
// silently losing an outgoing report or discarding an incoming one; STENO_SEED makes the pattern repeatable.
const dropWrites = Number(process.env.STENO_DROP_WRITES || 0), dropReads = Number(process.env.STENO_DROP_READS || 0);
let seed = Number(process.env.STENO_SEED || 1) >>> 0;
const random = () => { seed = (Math.imul(seed, 1664525) + 1013904223) >>> 0; return seed / 4294967296; };
const dropped = {writes: 0, reads: 0};
const link = {
  async write(report) {
    if (dropWrites && random() < dropWrites) { dropped.writes++; return; }
    send("W", Buffer.from(report));
  },
  async read(timeoutMs) {
    send("R", u16(Math.min(65535, Math.max(0, Math.round(timeoutMs)))));
    const tag = (await expect(1))[0];
    if (tag !== 0x52) return null;
    const report = new Uint8Array(await expect(63));
    if (dropReads && random() < dropReads) { dropped.reads++; return null; }
    return report;
  },
};
const hostConsole = {
  async command(text) {
    const body = Buffer.from(text);
    send("C", u16(body.length), body);
    await expect(1);
    const length = (await expect(2)).readUInt16LE();
    return (await expect(length)).toString();
  },
};

const spec = JSON.parse(fs.readFileSync(updatePath, "utf8"));
const update = {
  segments: spec.segments.map(([segment, hex]) => [segment, Uint8Array.from(Buffer.from(hex, "hex"))]),
  metadata: spec.metadata,
  payload: Uint8Array.from(Buffer.from(spec.payload, "hex")),
};
const stats = {};
const messages = [];
let result;
try {
  await upload(hostConsole, link, update, (text) => messages.push(text), {stats});
  result = {stats, messages, dropped};
} catch (error) {
  result = {error: String(error?.message ?? error), stats, messages, dropped};
}
const json = Buffer.from(JSON.stringify(result));
const length = Buffer.alloc(4);
length.writeUInt32LE(json.length);
send("D", length, json);
process.stdin.pause();
