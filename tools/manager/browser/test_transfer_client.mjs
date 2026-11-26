// node tools/manager/browser/test_transfer_client.mjs   (no browser, no hardware)
// Scripted keyboards for the protocol corner cases. The scripted cases are ports of
// tools/tests/test_native_transfer.py, so the JS client is held to the same rules as the Python one.
import assert from "node:assert/strict";
import {upload, records, probe, TransferUnavailable, NAK, STATE} from "./transfer_client.js";
import {Decoder, Type, encode} from "./transfer_frame.js";

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

class ScriptLink {
  constructor(handler) { this.handler = handler; this.decoder = new Decoder(); this.queue = []; this.seen = []; this.session = 1; }
  async write(report) {
    this.decoder.push(report);
    for (const frame of this.decoder.frames()) {
      this.session = frame.session;
      this.seen.push(frame);
      for (const [type, offset, payload] of this.handler(frame)) this.reply(type, offset, payload);
    }
  }
  reply(type, offset, payload = new Uint8Array(0)) {
    const report = new Uint8Array(63);
    report.set(encode(type, this.session, offset, payload));
    this.queue.push(report);
  }
  async read(timeoutMs) {
    if (this.queue.length) return this.queue.shift();
    await sleep(Math.min(timeoutMs, 0.5));
    return null;
  }
}

const console_ = (capable = true) => ({
  commands: [],
  async command(text) {
    this.commands.push(text);
    if (text.includes("transfer-capabilities")) return capable ? "OK XFER1 49" : "ERR Unknown native update command";
    if (text.includes("transfer-kind")) return "OK 0";
    return "OK";
  },
});

const fakeUpdate = () => ({segments: new Map([[2, Uint8Array.from({length: 1024}, (_, i) => i & 255)]]), metadata: 2, payload: new Uint8Array(64)});
const ack = (status, offset, credit = 8) => [Type.ACK, offset, Uint8Array.of(status, credit & 255, credit >> 8)];
const nak = (offset, reason) => [Type.NAK, offset, Uint8Array.of(reason)];
const done = (offset) => [Type.DONE, offset, new Uint8Array(0)];
const bytes = (frame) => frame.payload;
const total = records(fakeUpdate()).length;

async function guarded(promise, ms = 10000) {
  let timer;
  const timeout = new Promise((_, reject) => { timer = setTimeout(() => reject(new Error("upload did not return")), ms); });
  try { return await Promise.race([promise, timeout]); } finally { clearTimeout(timer); }
}
async function run(handler, options = {}) {
  const link = new ScriptLink(handler);
  await guarded(upload(console_(), link, fakeUpdate(), () => {}, options));
  return link;
}

const tests = [];
const test = (name, body) => tests.push([name, body]);

test("records: ERASE then non-blank 256-byte WRITE records, blank chunks skipped", () => {
  const update = {segments: [[3, new Uint8Array(512).fill(0xff)], [2, Uint8Array.from({length: 512}, (_, i) => (i < 256 ? 0xff : 7))]], metadata: 0, payload: new Uint8Array(0)};
  const out = records(update);
  // seg 3: ERASE only (blank); seg 2: ERASE then one WRITE at 2*65536+256, 256 bytes.
  assert.deepEqual(Array.from(out.subarray(0, 3)), [1, 3, 0]);
  assert.deepEqual(Array.from(out.subarray(3, 6)), [1, 2, 0]);
  const view = new DataView(out.buffer, out.byteOffset);
  assert.equal(out[6], 2);
  assert.equal(view.getUint32(7, true), 2 * 65536 + 256);
  assert.equal(view.getUint16(11, true), 256);
  assert.equal(out.length, 6 + 7 + 256);
  assert.throws(() => records({segments: [[2, Uint8Array.of(1, 2, 3)]]}), /multiple of four/);
});

test("probe reports the chunk and kind; a keyboard without the transfer is TransferUnavailable", async () => {
  assert.deepEqual(await probe(console_()), {chunk: 49, kind: 0});
  await assert.rejects(probe(console_(false)), TransferUnavailable);
});

test("commit waits for a real verified ack", async () => {
  const ends = [];
  const link = await run((frame) => {
    if (frame.type === Type.BEGIN) return [ack(STATE.OPEN, 0)];
    if (frame.type === Type.DATA) return [ack(STATE.RECEIVING, frame.offset + frame.payload.length)];
    if (frame.type === Type.END) {
      ends.push(frame.offset);
      if (ends.length === 1) return [nak(frame.offset, NAK.SINK_BUSY)];
      if (ends.length === 2) return [ack(STATE.RECEIVING, frame.offset)]; // A straggler ACK is not verification.
      return [ack(STATE.VERIFIED, frame.offset)];
    }
    return [done(frame.offset)];
  });
  const order = link.seen.map((f) => f.type);
  assert.equal(ends.length, 3);
  assert.equal(order.filter((t) => t === Type.COMMIT).length, 1);
  assert.ok(order.indexOf(Type.COMMIT) > order.lastIndexOf(Type.END));
});

test("a lost frame is recovered by the NAK, not by the stall timer", async () => {
  const state = {dropped: false, next: 0};
  const started = performance.now();
  const link = await run((frame) => {
    if (frame.type === Type.BEGIN) return [ack(STATE.OPEN, 0, 4)];
    if (frame.type === Type.DATA) {
      if (frame.offset === 98 && !state.dropped) { state.dropped = true; return []; }
      if (frame.offset !== state.next) return [nak(state.next, NAK.OUT_OF_ORDER)];
      state.next = frame.offset + frame.payload.length;
      return [ack(STATE.RECEIVING, state.next, 4)];
    }
    if (frame.type === Type.END) return [ack(STATE.VERIFIED, frame.offset)];
    return [done(frame.offset)];
  }, {window: 4, stallMs: 30000});
  assert.ok(performance.now() - started < 5000); // The stall timer would take 30 s.
  assert.ok(link.seen.filter((f) => f.type === Type.DATA).length < 40); // One rewind, not a retransmission storm.
});

test("endless busy NAKs are not progress", async () => {
  const link = new ScriptLink((frame) => (frame.type === Type.BEGIN ? [ack(STATE.OPEN, 0)] : []));
  const read = link.read.bind(link);
  link.read = async (timeoutMs) => { // A keyboard that answers every read with a busy NAK, whatever the host sent.
    if (!link.queue.length) link.reply(Type.NAK, 0, Uint8Array.of(NAK.SINK_BUSY));
    return read(timeoutMs);
  };
  await assert.rejects(guarded(upload(console_(), link, fakeUpdate(), () => {}, {stallMs: 50, retries: 3}), 10000), /stopped responding/);
});

test("a NAK offset beyond what was sent is ignored", async () => {
  const state = {next: 0, lied: false};
  await run((frame) => {
    if (frame.type === Type.BEGIN) return [ack(STATE.OPEN, 0, 4)];
    if (frame.type === Type.DATA) {
      if (!state.lied) { state.lied = true; return [nak(500, NAK.SINK_BUSY)]; } // In range but impossible: far beyond anything sent.
      if (frame.offset !== state.next) return [nak(state.next, NAK.OUT_OF_ORDER)];
      state.next = frame.offset + frame.payload.length;
      return [ack(STATE.RECEIVING, state.next, 4)];
    }
    if (frame.type === Type.END) return state.next !== total ? [[Type.ERROR, 0, Uint8Array.of(4)]] : [ack(STATE.VERIFIED, frame.offset)];
    return [done(frame.offset)];
  }, {window: 4, stallMs: 200});
  assert.equal(state.next, total);
});

test("a busy NAK carrying the receiver's offset resumes exactly there", async () => {
  const state = {next: 0, busyOnce: true};
  await run((frame) => {
    if (frame.type === Type.BEGIN) return [ack(STATE.OPEN, 0, 4)];
    if (frame.type === Type.DATA) {
      if (frame.offset === 49 && state.busyOnce) { state.busyOnce = false; return [nak(state.next, NAK.SINK_BUSY)]; } // The receiver's offset, not 49.
      if (frame.offset !== state.next) return [nak(state.next, NAK.OUT_OF_ORDER)];
      state.next = frame.offset + frame.payload.length;
      return [ack(STATE.RECEIVING, state.next, 4)];
    }
    if (frame.type === Type.END) return state.next === total ? [ack(STATE.VERIFIED, frame.offset)] : [[Type.ERROR, 0, Uint8Array.of(4)]];
    return [done(frame.offset)];
  }, {window: 4, stallMs: 5000});
  assert.equal(state.next, total);
});

test("an ERROR frame aborts and is reported", async () => {
  const link = new ScriptLink((frame) => {
    if (frame.type === Type.BEGIN) return [ack(STATE.OPEN, 0)];
    if (frame.type === Type.DATA) return [[Type.ERROR, frame.offset, Uint8Array.of(5)]];
    return [];
  });
  await assert.rejects(upload(console_(), link, fakeUpdate(), () => {}), /checksum/);
  assert.equal(link.seen.at(-1).type, Type.ABORT);
});

// A receiver that follows the cumulative rule of src/steno_squid/transfer_session.h exactly.
function cumulativeReceiver(window, {dropAckAt = -1} = {}) {
  const state = {next: 0, unacked: 0, acks: 0, dropped: false, dataFrames: 0};
  const every = window >= 2 ? Math.floor(window / 2) : 1;
  state.handler = (frame) => {
    if (frame.type === Type.BEGIN) return [ack(STATE.OPEN, 0, window)];
    if (frame.type === Type.DATA) {
      state.dataFrames++;
      if (frame.offset + frame.payload.length <= state.next) { state.acks++; return [ack(STATE.RECEIVING, state.next, window)]; } // Duplicate: always acked.
      if (frame.offset > state.next) return [nak(state.next, NAK.OUT_OF_ORDER)];
      if (frame.offset < state.next) return [nak(state.next, NAK.BAD_OFFSET)];
      state.next += frame.payload.length;
      if (++state.unacked >= every || state.next === total || frame.payload.length < 49) {
        state.unacked = 0;
        if (state.next === dropAckAt && !state.dropped) { state.dropped = true; return []; } // This acknowledgement is lost.
        state.acks++;
        return [ack(STATE.RECEIVING, state.next, window)];
      }
      return [];
    }
    if (frame.type === Type.END) return state.next === total ? [ack(STATE.VERIFIED, frame.offset)] : [nak(state.next, NAK.OUT_OF_ORDER)];
    return [done(frame.offset)];
  };
  return state;
}

test("cumulative acknowledgements: completes with no stall, no resend, one ack per window/2 frames", async () => {
  for (const window of [1, 2, 3, 4, 8, 16]) {
    const receiver = cumulativeReceiver(window);
    const link = await run(receiver.handler, {window, stallMs: 5000});
    const frames = Math.ceil(total / 49);
    assert.equal(receiver.dataFrames, frames, `window ${window}: no frame was resent`);
    const every = window >= 2 ? Math.floor(window / 2) : 1;
    assert.equal(receiver.acks, Math.floor(frames / every) + (frames % every ? 1 : 0), `window ${window}`);
    assert.equal(link.seen.filter((f) => f.type === Type.COMMIT).length, 1);
  }
});

test("a lost cumulative acknowledgement is recovered by the stall timer and a duplicate ack", async () => {
  const receiver = cumulativeReceiver(8, {dropAckAt: 49 * 4});
  const started = performance.now();
  await run(receiver.handler, {window: 8, stallMs: 60, retries: 50});
  assert.ok(receiver.dropped);
  assert.ok(performance.now() - started < 3000);
  assert.equal(receiver.next, total);
});

test("a keyboard that refuses BEGIN before any data is TransferUnavailable (the caller may use Base64)", async () => {
  const link = new ScriptLink((frame) => (frame.type === Type.BEGIN ? [[Type.ERROR, 0, Uint8Array.of(1)]] : []));
  await assert.rejects(upload(console_(), link, fakeUpdate(), () => {}), TransferUnavailable);
  assert.equal(link.seen.filter((f) => f.type === Type.DATA).length, 0);
});

test("silence after BEGIN, then a dead link mid-transfer, raises and never falls back", async () => {
  let writes = 0;
  const link = new ScriptLink(() => []);
  await assert.rejects(upload(console_(), link, fakeUpdate(), () => {}), TransferUnavailable); // No reply to BEGIN.
  const state = cumulativeReceiver(8);
  const dying = new ScriptLink((frame) => (++writes > 6 ? [] : state.handler(frame)));
  const c = console_();
  await assert.rejects(guarded(upload(c, dying, fakeUpdate(), () => {}, {stallMs: 10, retries: 3})), /stopped responding/);
  assert.ok(!c.commands.some((text) => /native_install update (write|erase|commit)/.test(text)));
});

let failed = 0;
for (const [name, body] of tests) {
  try { await body(); console.log(`ok   ${name}`); } catch (error) { failed++; console.log(`FAIL ${name}\n     ${error.stack}`); }
}
if (failed) { console.log(`${failed} FAILED`); process.exit(1); }
console.log(`PASS transfer client (JS), ${tests.length} cases`);
