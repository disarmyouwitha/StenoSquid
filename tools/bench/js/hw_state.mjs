// Lab tool, run by tools/bench/run_state_hw.py: runs the REAL browser console (console.js) and state reader (state.js) against a
// real keyboard. Node has no HID, so Python relays reports over this process's stdio (the same relay as hw_upload.mjs):
//   node -> python:  'W' + 63 bytes          write one report (no reply)
//                    'R' + u16 timeout ms    read one report   -> 'R' + 63 bytes | 'N'
//                    'D' + u32 n + JSON      finished ({view, progress, shown, sleeps, ms} or {error})
// argv: serial, transport, action
import {Console} from "../../manager/browser/console.js";
import {loadView} from "../../manager/browser/state.js";

const [serial, transport, action] = process.argv.slice(2);
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

const link = {
  async write(report) { send("W", Buffer.from(report)); },
  async read(timeoutMs) {
    send("R", u16(Math.min(65535, Math.max(0, Math.round(timeoutMs)))));
    const tag = (await expect(1))[0];
    if (tag !== 0x52) return [];
    return Array.from(await expect(63));
  },
};

const progress = [], shown = [], sleeps = [];
const started = performance.now();
let out;
try {
  const console_ = new Console(link, {serial, transport});
  const deps = {lastRepackBackup: async () => null, sleep: async (seconds) => { sleeps.push(seconds); await new Promise((r) => setTimeout(r, seconds * 1000)); }};
  const view = await loadView(console_, action, (message) => progress.push(message), (value) => shown.push(JSON.parse(JSON.stringify(value))), deps);
  out = {view: JSON.parse(JSON.stringify(view)), progress, shown, sleeps, ms: Math.round(performance.now() - started)};
} catch (error) {
  out = {error: `${error.name}: ${error.message}`, progress, sleeps, ms: Math.round(performance.now() - started)};
}
const body = Buffer.from(JSON.stringify(out));
const size = Buffer.alloc(4);
size.writeUInt32LE(body.length);
send("D", size, body);
process.stdin.pause();
