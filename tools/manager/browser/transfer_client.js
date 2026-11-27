// Browser-side client for the experimental binary transfer session (upload direction).
// A port of tools/storage/native_transfer.py: same wire format, same go-back-N window, same stall rule
// ("the acknowledged offset did not advance"), same refusal handling. It talks to the keyboard through a
// small link so it runs unchanged on WebHID, on the C++ device simulator, or on a scripted test device:
//
//   link.write(report)      -> Promise; report is a 63-byte Uint8Array (one frame, zero padded)
//   link.read(timeoutMs)    -> Promise<Uint8Array | null>; the next input report, or null on timeout
//
// The console (text commands) is separate and only used to negotiate:
//
//   console.command(text, timeoutMs) -> Promise<string>
//
// Acknowledgements are cumulative (docs/transfer-session.md): the keyboard replies every window/2 frames, on
// the last frame and on a short frame, so the client never expects one ACK per frame.
import {Decoder, Type, MAX_PAYLOAD, HEADER, TRAILER, crc32, encode} from "./transfer_frame.js";

const REPORT_BYTES = 63;
const SEGMENT = 65536;
const WRITE_BYTES = 256;
const MAX_WINDOW = 16;
const RECORD_ERASE = 1, RECORD_WRITE = 2;
const NAK_OUT_OF_ORDER = 1, NAK_BAD_OFFSET = 2, NAK_SINK_BUSY = 3;
const STATE_OPEN = 1, STATE_RECEIVING = 2, STATE_VERIFIED = 3;
export const NAK = {OUT_OF_ORDER: NAK_OUT_OF_ORDER, BAD_OFFSET: NAK_BAD_OFFSET, SINK_BUSY: NAK_SINK_BUSY};
export const STATE = {OPEN: STATE_OPEN, RECEIVING: STATE_RECEIVING, VERIFIED: STATE_VERIFIED};

const ERROR_TEXT = {
  1: "another transfer is active", 2: "the transfer session was lost", 3: "unexpected transfer state",
  4: "invalid transfer length", 5: "the data failed its checksum", 6: "the keyboard refused the data",
  7: "the keyboard could not verify the staged data", 8: "the keyboard could not save the catalog",
  9: "the transfer timed out", 10: "the transfer was cancelled", 11: "invalid transfer parameters",
};

// Nothing was sent that matters: the caller may use the Base64 console path.
export class TransferUnavailable extends Error {}

const now = () => performance.now();
const defaultSleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const errorFor = (code) => Object.assign(new Error(`Binary transfer failed: ${ERROR_TEXT[code] ?? "error " + code}.`), {kind: "RuntimeError"});

// The staged update as ERASE/WRITE records, in the order the Base64 path would send them.
// update.segments: Map or array of [segment, Uint8Array] pairs.
export function records(update) {
  const parts = [];
  let total = 0;
  const push = (bytes) => { parts.push(bytes); total += bytes.length; };
  for (const [segment, block] of update.segments) {
    const erase = new Uint8Array(3);
    erase[0] = RECORD_ERASE;
    new DataView(erase.buffer).setUint16(1, segment, true);
    push(erase);
    for (let offset = 0; offset < block.length; offset += WRITE_BYTES) {
      const chunk = block.subarray(offset, Math.min(block.length, offset + WRITE_BYTES));
      if (chunk.every((byte) => byte === 0xff)) continue;
      if (chunk.length % 4) throw new RangeError("Staged data must be a multiple of four bytes");
      const header = new Uint8Array(7);
      header[0] = RECORD_WRITE;
      const view = new DataView(header.buffer);
      view.setUint32(1, segment * SEGMENT + offset, true);
      view.setUint16(5, chunk.length, true);
      push(header);
      push(chunk);
    }
  }
  const out = new Uint8Array(total);
  let at = 0;
  for (const part of parts) { out.set(part, at); at += part.length; }
  return out;
}

// Chunk size and staged-update kind, or TransferUnavailable. Sends nothing destructive.
export async function probe(console) {
  let capabilities, kind;
  try {
    capabilities = await console.command("native_install update transfer-capabilities", 3000);
    const match = /^OK XFER1 (\d+)$/.exec(capabilities);
    if (!match) throw new TransferUnavailable(capabilities);
    kind = /^OK (\d+)$/.exec(await console.command("native_install update transfer-kind", 3000));
    if (!kind) throw new TransferUnavailable("no staged update");
    const chunk = Number(match[1]);
    if (!(chunk >= 1 && chunk <= Math.min(MAX_PAYLOAD, REPORT_BYTES - HEADER - TRAILER))) {
      throw new TransferUnavailable("unsupported chunk size");
    }
    return {chunk, kind: Number(kind[1])};
  } catch (error) {
    if (error instanceof TransferUnavailable) throw error;
    throw new TransferUnavailable(String(error?.message ?? error));
  }
}

class Session {
  constructor(link, id) { this.link = link; this.id = id; this.decoder = new Decoder(); }
  async send(type, offset, payload = new Uint8Array(0)) {
    const report = new Uint8Array(REPORT_BYTES);
    report.set(encode(type, this.id, offset, payload));
    await this.link.write(report);
  }
  async receive(timeoutMs) {
    const packet = await this.link.read(timeoutMs);
    if (!packet || !packet.length) return [];
    this.decoder.push(packet);
    return [...this.decoder.frames()].filter((frame) => frame.session === this.id);
  }
}

function beginPayload(total, streamCrc, chunk, window, kind, update) {
  const out = new Uint8Array(13 + 12);
  const view = new DataView(out.buffer);
  view.setUint32(0, total, true);
  view.setUint32(4, streamCrc, true);
  view.setUint16(8, chunk, true);
  view.setUint16(10, window, true);
  out[12] = kind;
  view.setUint32(13, update.metadata, true);
  view.setUint32(17, update.payload.length, true);
  view.setUint32(21, crc32(update.payload), true);
  return out;
}

// Stream, verify and commit `update`. Resolves when the keyboard reports DONE.
export async function upload(console, link, update, progress = () => {}, options = {}) {
  const {window: requested = 8, stallMs = 1000, retries = 6, sleep = defaultSleep} = options;
  // Optional instrumentation: pass an object and it is filled with stage times (ms) and frame counters.
  const stats = options.stats ?? {};
  Object.assign(stats, {dataFrames: 0, acks: 0, naks: 0, rewinds: 0, stalls: 0});
  const started = now();
  const {chunk, kind} = await probe(console);
  const bytes = records(update);
  const total = bytes.length;
  const session = new Session(link, globalThis.crypto.getRandomValues(new Uint16Array(1))[0] || 1);
  let window = Math.max(1, Math.min(requested, MAX_WINDOW));
  const begin = beginPayload(total, crc32(bytes), chunk, window, kind, update);

  let opened = false;
  for (let attempt = 0; attempt < 3 && !opened; attempt++) { // BEGIN is idempotent until data arrives.
    await session.send(Type.BEGIN, 0, begin);
    const reply = (await session.receive(1000))[0];
    if (!reply) continue;
    if (reply.type === Type.ACK && reply.payload[0] === STATE_OPEN) {
      window = Math.max(1, Math.min(window, new DataView(reply.payload.buffer, reply.payload.byteOffset).getUint16(1, true)));
      opened = true;
    } else {
      throw new TransferUnavailable(reply.type === Type.ERROR ? (ERROR_TEXT[reply.payload[0]] ?? "refused") : "refused");
    }
  }
  if (!opened) throw new TransferUnavailable("no reply to BEGIN");

  try {
    const streaming = now();
    stats.beginMs = streaming - started;
    stats.streamBytes = total;
    await stream(session, bytes, total, chunk, window, stallMs, retries, progress, sleep, stats);
    stats.streamMs = now() - streaming;
    await finish(session, total, sleep, stats);
    stats.totalMs = now() - started;
  } catch (error) {
    try { await session.send(Type.ABORT, 0); } catch { /* the link is gone; the keyboard times the session out */ }
    throw error;
  }
}

async function stream(session, bytes, total, chunk, window, stallMs, retries, progress, sleep, stats) {
  let acked = 0, sent = 0, lastPercent = -5, stalls = 0;
  let lastProgress = now();
  let lastRewind = [-1, 0];
  progress("Writing dictionary data… 0%");
  while (acked < total) {
    while (sent < total && sent - acked < window * chunk) {
      await session.send(Type.DATA, sent, bytes.subarray(sent, sent + chunk));
      stats.dataFrames++;
      sent = Math.min(total, sent + chunk);
    }
    for (const {type, offset, payload} of await session.receive(50)) {
      if (type === Type.ERROR) throw errorFor(payload[0]);
      // The keyboard cannot have accepted more than was sent; a larger offset is noise, not progress.
      if ((type !== Type.ACK && type !== Type.NAK) || offset > sent) continue;
      if (type === Type.ACK) stats.acks++; else stats.naks++;
      if (offset > acked) { acked = offset; stalls = 0; lastProgress = now(); }
      if (type === Type.NAK) {
        // One rewind per gap: later NAKs for frames already in flight are noise.
        const at = now();
        if (acked !== lastRewind[0] || at - lastRewind[1] > 200) {
          sent = acked;
          stats.rewinds++;
          lastRewind = [acked, at];
          if (payload[0] === NAK_SINK_BUSY) await sleep(2);
        }
      }
    }
    const percent = Math.floor(acked * 100 / total);
    if (percent >= lastPercent + 5 || (percent === 100 && lastPercent !== 100)) {
      progress(`Writing dictionary data… ${percent}%`);
      lastPercent = percent;
    }
    // Progress means the acknowledged offset advanced. Replies that do not advance it (an endless stream of
    // busy NAKs, say) must not keep the transfer alive.
    if (now() - lastProgress > stallMs) {
      stats.stalls++;
      if (++stalls > retries) throw new Error("The keyboard stopped responding during the transfer. Check the connection.");
      sent = acked; // Go back N: resend from the last acknowledged byte.
      lastProgress = now();
    }
  }
}

// Send `resend`, then wait for a frame in `wanted`. END and COMMIT are idempotent, so a silent link is probed
// by repeating them every `everyMs` (slowly for COMMIT: the keyboard is busy and cannot answer). A NAK
// sink-busy asks for an immediate polite repeat.
async function waitFor(session, wanted, resend, deadline, sleep, everyMs) {
  await resend();
  let last = now();
  while (now() < deadline) {
    for (const {type, payload} of await session.receive(50)) {
      if (type === Type.ERROR) throw errorFor(payload[0]);
      if (wanted.includes(type)) return {type, payload};
      if (type === Type.NAK) { await sleep(2); await resend(); last = now(); }
    }
    if (now() - last >= everyMs) { await resend(); last = now(); }
  }
  throw new Error("No reply from the keyboard during the transfer.");
}

async function finish(session, total, sleep, stats) {
  const verifyStart = now();
  const verifyDeadline = verifyStart + 120000;
  for (;;) { // Verify runs in bounded slices on the keyboard; each busy NAK asks us to repeat END.
    const {payload} = await waitFor(session, [Type.ACK], () => session.send(Type.END, total), verifyDeadline, sleep, 500);
    if (payload[0] === STATE_VERIFIED) break;
  }
  const commitStart = now();
  stats.verifyMs = commitStart - verifyStart;
  await waitFor(session, [Type.DONE], () => session.send(Type.COMMIT, total), commitStart + 1800000, sleep, 2000);
  stats.commitMs = now() - commitStart;
}

// Adapter from tools/manager/browser/hid.js HidConnection (write(array), read(timeout) -> array) to a link.
export function linkFromHid(connection) {
  return {
    write: (report) => connection.write(report),
    read: async (timeoutMs) => { const packet = await connection.read(timeoutMs); return packet?.length ? Uint8Array.from(packet) : null; },
  };
}
