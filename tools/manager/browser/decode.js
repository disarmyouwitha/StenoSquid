// Manager milestone M1 (lookup/verify reader): enumerate a compiled JSC4 dictionary and merge a snapshot's journal edits
// over it, ported from tools/storage/hybrid.py (format_stroke, compact_entries), tools/dict_compiler/jsc4.py
// (reader checks, require_snapshot_hint) and effective_entries in tools/storage/native_update.py. It keeps Python's
// validation, so a damaged image is rejected rather than half-decoded; tools/manager/browser/test_decode.mjs compares
// results and error messages with golden vectors from golden_decode.py. Supersedes tools/bench/js/decode_dict.mjs.
import {assertionError} from "./errors.js";
import {crc32} from "./transfer_frame.js";
import {SEGMENT, rowName} from "./snapshot.js";

export const XIP = 0x12000000;
export class Corrupt extends Error {}

const LEFT = "STKPWHR", MIDDLE = "AO*EU", RIGHT = "FRPBLGTSDZ";

export function formatStroke(mask) {
  if (!mask || mask >>> 25) throw new Corrupt("Unsupported compact stroke");
  let left = "", middle = "", right = "";
  for (let i = 0; i < 7; i++) if (mask & (1 << (1 + i))) left += LEFT[i];
  for (let i = 0; i < 5; i++) if (mask & (1 << (8 + i))) middle += MIDDLE[i];
  for (let i = 0; i < 10; i++) if (mask & (1 << (13 + i))) right += RIGHT[i];
  return (mask & 1 ? "#" : "") + (mask & (1 << 23) ? "^" : "") + (mask & (1 << 24) ? "+" : "") + left + middle + (right && !middle ? "-" : "") + right;
}

const popcount = (v) => { v -= (v >>> 1) & 0x55555555; v = (v & 0x33333333) + ((v >>> 2) & 0x33333333); return (((v + (v >>> 4)) & 0x0f0f0f0f) * 0x01010101) >>> 24; };

function accessors(blob, base) {
  const view = new DataView(blob.buffer, blob.byteOffset, blob.byteLength);
  const word = (offset) => view.getUint32(offset, true); // throws RangeError past the end, like struct.error
  const byteAt = (offset) => {
    if (offset >= blob.length || offset < 0) throw new RangeError("index out of range");
    return blob[offset];
  };
  // Python slices stop at the end of the image; int.from_bytes of a short slice equals the zero-extended value.
  const u24 = (offset) => (blob[offset] | (blob[offset + 1] << 8) | (blob[offset + 2] << 16)) >>> 0 || 0;
  const rawPointer = (offset) => word(offset) - base;
  return {word, byteAt, u24, rawPointer};
}

// jsc4.reader's structural checks (it also builds a lookup function, which the Manager does not need here).
function readerChecks(blob, base, index, count) {
  const {word, byteAt} = accessors(blob, base);
  const pointer = (offset) => {
    const value = word(offset) - base;
    if (!(value >= 0 && value < blob.length)) throw assertionError(`Pointer outside image at 0x${offset.toString(16)}`);
    return value;
  };
  if (blob.length % 4 !== 0) throw assertionError("assertion failed");
  if (!(String.fromCharCode(...blob.subarray(0, 4)) === "JSC4" && word(4) === count && index < count)) throw assertionError("assertion failed");
  if (word(32) !== word(pointer(12) + word(8))) throw assertionError("Timestamp mismatch");
  const definition = pointer(36 + 4 * index);
  const maximum = byteAt(definition + 1);
  if (!(maximum > 0 && maximum <= 16)) throw assertionError("assertion failed");
  pointer(definition + 8);
  pointer(definition + 12);
}

// Every (outline, text) of one member, in table order. The lazy form yields one entry at a time like Python's generator, so a
// caller that verifies each entry sees a failure in the same order; compactEntries returns them all. `count` is the number of members in the image.
export function* compactEntriesLazy(blob, base, member = 0, count = 1) {
  readerChecks(blob, base, member, count);
  const {word, byteAt, u24} = accessors(blob, base);
  const pointer = (offset) => {
    const value = word(offset) - base;
    if (!(value >= 0 && value < blob.length)) throw new Corrupt("Compact pointer bounds");
    return value;
  };
  const decoder = new TextDecoder("utf-8", {fatal: true});
  const names = new Map();
  const name = (mask) => { let s = names.get(mask); if (s === undefined) names.set(mask, s = formatStroke(mask)); return s; };
  const definition = pointer(36 + 4 * member);
  const text = pointer(definition + 8), table = pointer(definition + 12), maximum = byteAt(definition + 1);
  if (!(maximum >= 1 && maximum <= 16) || table + 12 * maximum > blob.length) throw new Corrupt("Compact stroke table bounds");
  for (let length = 1; length <= maximum; length++) {
    const entry = table + 12 * (length - 1), mask = word(entry);
    if (!mask) continue;
    const data = pointer(entry + 4), blocks = pointer(entry + 8), blockCount = Math.floor((mask + 1) / 128);
    if ((mask + 1) % 128 || ((mask + 1) & mask) || blocks + 20 * blockCount > blob.length) throw new Corrupt("Compact bitmap bounds");
    let entries = 0;
    for (let b = 0; b < blockCount; b++) for (let i = 0; i < 4; i++) entries += popcount(word(blocks + 20 * b + 4 * i));
    const stride = 3 + 3 * length;
    if (data + entries * stride > blob.length) throw new Corrupt("Compact entries outside image");
    for (let index = 0; index < entries; index++) {
      const position = data + index * stride;
      const start = text + u24(position);
      if (!(text <= start && start < blob.length)) throw new Corrupt("Compact text outside image");
      const end = blob.indexOf(0, start);
      if (end < 0) throw new Error("subsection not found");
      let outline = "";
      for (let i = 0; i < length; i++) outline += (i ? "/" : "") + name(u24(position + 3 + 3 * i));
      yield [outline, decoder.decode(blob.subarray(start, end))];
    }
  }
}

export const compactEntries = (...args) => [...compactEntriesLazy(...args)];

// A user snapshot is one JSC4 member ending at its SHI1 directory.
export function requireSnapshotHint(blob, base) {
  const fail = () => { throw new Error("Unsupported compact snapshot. A lean JSC4 image ending in SHI1 is required."); };
  if (blob.length >= 60 && blob.length % 4 === 0 && String.fromCharCode(...blob.subarray(0, 8)) === "JSC4\x01\0\0\0") {
    const view = new DataView(blob.buffer, blob.byteOffset, blob.byteLength);
    const magic = String.fromCharCode(...blob.subarray(blob.length - 12, blob.length - 8));
    const directory = view.getUint32(blob.length - 8, true), members = view.getUint32(blob.length - 4, true);
    if (magic === "SHI1" && members === 1 && directory === blob.length - 20) {
      const records = view.getUint32(directory, true), count = view.getUint32(directory + 4, true);
      const textBytes = view.getUint32(8, true), textPointer = view.getUint32(12, true);
      const text = textPointer - base;
      if (text >= 0 && text <= records && records % 4 === 0 && text + textBytes + 4 <= records && records + count * 8 === directory) return;
    }
  }
  fail();
}

// One logical dictionary as a Map(outline -> text): its compiled base (if any) with the snapshot's journal edits applied.
// `read(address, length)` may be async. The snapshot comes from Snapshot.load (normal, not metadata-only).
export async function effectiveEntries(snapshot, name, read) {
  const row = snapshot.rows.find((candidate) => rowName(candidate) === name);
  if (row === undefined) throw new Error(`No installed dictionary named '${name}'`);
  const view = new DataView(row.buffer, row.byteOffset, row.byteLength);
  const identity = view.getUint32(0, true), base = view.getUint16(8, true), member = row[10];
  const entries = new Map();
  if (base !== 65535) {
    const [segment, , size, checksum] = snapshot.extents[base];
    const address = segment * SEGMENT;
    const blob = Uint8Array.from(await read(address, size));
    if (crc32(blob) !== checksum) throw new Error("Compiled dictionary checksum failed during export.");
    if (row[11]) requireSnapshotHint(blob, XIP + address);
    const count = new DataView(blob.buffer).getUint16(4, true);
    for (const [outline, text] of compactEntries(blob, XIP + address, member, count)) entries.set(outline, text);
  }
  const decoder = new TextDecoder("utf-8", {fatal: true});
  for (const entry of snapshot.entries.values()) {
    if (entry.identity !== identity) continue;
    const outline = entry.strokes.map(formatStroke).join("/");
    if (entry.operation) entries.delete(outline);
    else entries.set(outline, decoder.decode(entry.text));
  }
  return entries;
}
