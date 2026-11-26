// Manager milestone M2: the named-snapshot, change/merge and import planners, ported from tools/storage/native_snapshot.py
// (plan_named_snapshot, plan_change, merge_rows, merged_entries, journal_baseline, the stage allocator, the token digests)
// and tools/storage/native_import_plan.py (plan_compact_import, consolidation_cost). Compiled images arrive as plain
// data ({data, pointers}); compiling is a separate step. Same checks in the same order, same bytes; golden vectors from
// tools/manager/browser/golden_snapshot_plan.py gate it and the Python code is the oracle.
import {crc32} from "./transfer_frame.js";
import {Snapshot, SEGMENT, OUTLINE_FIRST, rowName, validateName, outlineKey, textKey, compareKeys} from "./snapshot.js";
import {NoSpace} from "./plan.js";
import {effectiveEntries, XIP} from "./decode.js";

export const SNAPSHOT_MARKER = 1;
const ASCII = (text) => Uint8Array.from(text, (c) => c.charCodeAt(0));
const view = (bytes) => new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
const concat = (...parts) => {
  const out = new Uint8Array(parts.reduce((sum, part) => sum + part.length, 0));
  let at = 0;
  for (const part of parts) { out.set(part, at); at += part.length; }
  return out;
};
const u32 = (bytes, offset) => view(bytes).getUint32(offset, true);
const u16 = (bytes, offset) => view(bytes).getUint16(offset, true);

export class CompactImage {
  constructor(data, pointers) { this.data = data; this.pointers = pointers; }
  // The image relocated to a flash address, as a copy.
  at(address) {
    const data = Uint8Array.from(this.data);
    const v = view(data);
    for (const offset of this.pointers) {
      const pointer = v.getUint32(offset, true);
      if (pointer >= data.length) throw new Error("Compiler pointer outside the snapshot");
      v.setUint32(offset, address + pointer, true);
    }
    return data;
  }
}

function packExtent(segment, span, bytes, crc, kind, auxiliary) {
  const out = new Uint8Array(24);
  const v = view(out);
  v.setUint16(0, segment, true); v.setUint16(2, span, true); v.setUint32(4, bytes, true);
  v.setUint32(8, crc, true); v.setUint32(12, kind, true); v.setBigUint64(16, BigInt(auxiliary), true);
  return out;
}

export function stageAllocator(protectedSegments, poolEnd = 255) {
  const free = new Set();
  for (let segment = 2; segment < poolEnd; segment++) if (!protectedSegments.has(segment)) free.add(segment);
  return (count) => {
    if (free.size < count + 3) throw new NoSpace("Too little free space after protecting the three-segment reserve");
    for (const segment of [...free].sort((a, b) => a - b)) {
      let run = true;
      for (let i = 0; i < count; i++) if (!free.has(segment + i)) { run = false; break; }
      if (run) {
        for (let i = 0; i < count; i++) free.delete(segment + i);
        return segment;
      }
    }
    throw new NoSpace("Not enough contiguous staging space");
  };
}

export function stageAllocation(protectedSegments, poolEnd = 255) {
  const allocate = stageAllocator(protectedSegments, poolEnd);
  const blocks = new Map(), extents = [];
  const write = (contents, kind) => {
    const compact = contents instanceof CompactImage;
    const size = compact ? contents.data.length : contents.length;
    const count = Math.floor((size + SEGMENT - 1) / SEGMENT);
    const segment = allocate(count);
    const data = compact ? contents.at(XIP + segment * SEGMENT) : contents;
    for (let i = 0; i < count; i++) blocks.set(segment + i, data.slice(i * SEGMENT, (i + 1) * SEGMENT));
    if (kind) extents.push(packExtent(segment, count, size, crc32(data), kind, kind === 3 ? OUTLINE_FIRST : 0));
    return segment;
  };
  return {blocks, extents, write};
}

const update = (blocks, metadata, payload) => ({segments: blocks, metadata, payload, reclaimed: 0, changes: 0});

export function planNamedSnapshot(snapshot, protectedSegments, name, compiled, limit = 16) {
  validateName(name);
  if (snapshot.rows.length >= limit || snapshot.nextId === 0xffffffff) throw new Error("The keyboard has reached its dictionary limit");
  if (new Set(snapshot.rows.map(rowName)).has(name)) throw new Error("Dictionary name already exists");
  if (snapshot.extents.length === 253) throw new NoSpace("Catalog extent limit");
  const {blocks, extents, write} = stageAllocation(protectedSegments, snapshot.poolEnd);
  write(compiled, 1);
  const row = new Uint8Array(96);
  const r = view(row);
  r.setUint32(0, snapshot.nextId, true); r.setUint32(4, 1, true); r.setUint16(8, snapshot.extents.length, true);
  row[10] = 0; row[11] = SNAPSHOT_MARKER;
  const label = ASCII(name);
  row[12] = label.length;
  row.set(label, 16);
  const prefix = snapshot.payload.slice(0, 32);
  const p = view(prefix);
  p.setUint32(0, snapshot.nextId + 1, true); p.setUint16(4, snapshot.rows.length + 1, true); p.setUint16(6, snapshot.extents.length + 1, true);
  const rows = [...snapshot.rows];
  const personal = rows.findIndex((old) => rowName(old) === "user_dictionary");
  rows.splice(personal + 1, 0, row);
  const originalExtents = snapshot.payload.subarray(32 + 96 * snapshot.rows.length);
  const payload = concat(prefix, ...rows, originalExtents, ...extents);
  const metadata = write(payload, 0);
  return update(blocks, metadata, payload);
}

const equalBytes = (a, b) => a.length === b.length && a.every((value, i) => value === b[i]);

export function mergeRows(snapshot, upperId, lowerId) {
  for (let i = 0; i + 1 < snapshot.rows.length; i++) {
    const upper = snapshot.rows[i], lower = snapshot.rows[i + 1];
    if (u32(upper, 0) !== upperId || u32(lower, 0) !== lowerId) continue;
    if (upper[11] === SNAPSHOT_MARKER && lower[11] === SNAPSHOT_MARKER
        && snapshot.personal !== upperId && snapshot.personal !== lowerId
        && equalBytes(upper.subarray(4, 8), lower.subarray(4, 8))
        && !equalBytes(upper.subarray(8, 10), lower.subarray(8, 10))) return [upper, lower];
  }
  throw new Error("Choose two adjacent added compact dictionaries with the same Active setting.");
}

export function planChange(snapshot, protectedSegments, identity, compiled = null, {consolidate = false, convert = false, clear = false, mergeId = null} = {}) {
  const deleting = compiled === null && !clear;
  if (compiled !== null && !(consolidate || convert)) throw new Error("Existing compact dictionaries can only be rebuilt by consolidation.");
  let target = snapshot.rows.find((row) => u32(row, 0) === identity) ?? null;
  if (target === null || (convert && (!(target[8] === 0xff && target[9] === 0xff && target[10] === 0 && target[11] === 0)
                                      || (identity >= 2 && identity <= 7) || deleting))) {
    throw new Error("Select a journal-only user dictionary");
  }
  if (clear && (target === null || rowName(target) !== "user_dictionary" || compiled !== null)) throw new Error("Select the built-in personal dictionary to clear");
  if (!(convert || clear) && (target[11] !== SNAPSHOT_MARKER || (deleting && rowName(target) === "user_dictionary"))) {
    throw new Error("Select an eligible compact user dictionary");
  }
  let retiring = null;
  if (mergeId !== null) {
    if (!consolidate || compiled === null || convert || clear) throw new Error("A merge requires a compiled replacement.");
    [target, retiring] = mergeRows(snapshot, identity, mergeId);
  }
  consolidate = consolidate || convert || clear;
  const oldBase = u16(target, 8);
  const retiringBase = retiring ? u16(retiring, 8) : null;
  const kept = [], remap = new Map();
  snapshot.extents.forEach((extent, i) => {
    if (i !== oldBase && i !== retiringBase && (!consolidate || extent[4] < 3)) {
      remap.set(i, kept.length);
      kept.push(packExtent(extent[0], extent[1], extent[2], extent[3], extent[4], extent[5]));
    }
  });
  const {blocks, extents: added, write} = stageAllocation(protectedSegments, snapshot.poolEnd);
  if (compiled !== null) {
    remap.set(oldBase, kept.length);
    write(compiled, 1);
  }
  if (consolidate) {
    const retained = [...snapshot.entries.values()].filter((entry) => (entry.identity !== identity && entry.identity !== mergeId) || (convert && entry.operation === 1));
    const forward = [...retained].sort((a, b) => compareKeys(outlineKey(a), outlineKey(b)));
    const reverse = retained.filter((entry) => !entry.operation).sort((a, b) => compareKeys(textKey(a), textKey(b)));
    for (const [kind, entries] of [[3, forward], [4, reverse]]) {
      if (entries.length) {
        const data = new Uint8Array(entries.length * 4);
        entries.forEach((entry, i) => view(data).setUint32(i * 4, entry.address, true));
        write(data, kind);
      }
    }
  }
  const extents = [...kept, ...added];
  if (extents.length > 253) throw new NoSpace("Catalog extent limit");
  const rows = [];
  for (const original of snapshot.rows) {
    if ((deleting && original === target) || original === retiring) continue;
    const row = Uint8Array.from(original);
    const base = u16(row, 8);
    if (original === target && clear) {
      view(row).setUint16(8, 65535, true); row[10] = 0; row[11] = 0;
    } else if (original === target && convert) {
      view(row).setUint16(8, remap.get(oldBase), true); row[10] = 0; row[11] = SNAPSHOT_MARKER;
    } else if (base !== 65535) {
      if (base === retiringBase || (base === oldBase && original !== target)) throw new Error("Snapshot unexpectedly shares its compact extent");
      view(row).setUint16(8, remap.get(base), true);
    }
    rows.push(row);
  }
  const prefix = snapshot.payload.slice(0, 32);
  view(prefix).setUint16(4, rows.length, true);
  view(prefix).setUint16(6, extents.length, true);
  if (consolidate) view(prefix).setBigUint64(8, snapshot.highwater, true);
  const payload = concat(prefix, ...rows, ...extents);
  const metadata = write(payload, 0);
  return update(blocks, metadata, payload);
}

// Both dictionaries' effective entries; the upper one wins overlaps. Returns {merged: Map, duplicates}.
export async function mergedEntries(snapshot, upper, lower, read) {
  const lowerEntries = await effectiveEntries(snapshot, rowName(lower), read);
  const upperEntries = await effectiveEntries(snapshot, rowName(upper), read);
  const merged = new Map([...lowerEntries, ...upperEntries]);
  return {merged, duplicates: upperEntries.size + lowerEntries.size - merged.size};
}

// Imported entries fill gaps; an existing local set or tombstone wins. additions: array of [strokes[], textBytes] or null.
export function journalBaseline(snapshot, identity, additions = null) {
  const keyOf = (strokes) => strokes.join(",");
  const effective = new Map();
  for (const [strokes, text] of additions ?? []) effective.set(keyOf(strokes), [strokes, text]);
  for (const entry of snapshot.entries.values()) {
    if (entry.identity !== identity) continue;
    if (entry.operation) effective.delete(keyOf(entry.strokes));
    else effective.set(keyOf(entry.strokes), [entry.strokes, entry.text]);
  }
  return [...effective.values()];
}
export const personalBaseline = (snapshot, additions = null) => journalBaseline(snapshot, snapshot.personal, additions);

export function inspectFree(protectedSegments, poolEnd = 255) {
  let free = 0, largest = 0, run = 0;
  for (let segment = 2; segment < poolEnd; segment++) {
    if (!protectedSegments.has(segment)) { free++; run++; largest = Math.max(largest, run); } else run = 0;
  }
  return [free, largest];
}

// ---- native_import_plan.py ----

export async function metadataSnapshot(payload, poolEnd = 255) {
  return Snapshot.load((address, size) => payload.subarray(0, size), 0, payload.length, crc32(payload), {metadataOnly: true, poolEnd});
}

export function ownedSegments(snapshot) {
  const owned = new Set([0, 1, snapshot.poolEnd]);
  for (const [start, count] of snapshot.extents) for (let segment = start; segment < start + count; segment++) owned.add(segment);
  return owned;
}

// A future same-size consolidation needs an image, up to two replacement indexes and staging metadata.
export function consolidationCost(after, imageSegments, indexRecords) {
  if (after.extents.filter((extent) => extent[4] < 3).length + indexRecords.filter(Boolean).length > 253) throw new NoSpace("Catalog extent limit");
  const allocate = stageAllocator(ownedSegments(after), after.poolEnd);
  allocate(imageSegments);
  let total = imageSegments;
  for (const count of indexRecords) {
    if (count) {
      const blocks = Math.floor((count * 4 + SEGMENT - 1) / SEGMENT);
      allocate(blocks);
      total += blocks;
    }
  }
  allocate(1);
  return total + 1;
}

export function planCompactImport(snapshot, protectedSegments, name, compiled, limit) {
  const report = {bytes: compiled.data.length, image_segments: Math.floor((compiled.data.length + SEGMENT - 1) / SEGMENT)};
  let result;
  try {
    result = planNamedSnapshot(snapshot, protectedSegments, name, compiled, limit);
  } catch (error) {
    if (error instanceof NoSpace) return [{...report, reason: error.message}, null];
    throw error;
  }
  return [{...report, staging_segments: result.segments.size}, result];
}

// ---- tokens: Python's json.dumps and hashlib.sha256, byte for byte ----

export function pyJson(value, separators = [", ", ": "]) {
  if (typeof value === "string") {
    let out = '"';
    for (const unit of value) {
      for (let i = 0; i < unit.length; i++) {
        const code = unit.charCodeAt(i);
        out += unit[i] === '"' ? '\\"' : unit[i] === "\\" ? "\\\\" : code === 10 ? "\\n" : code === 13 ? "\\r" : code === 9 ? "\\t"
          : code === 8 ? "\\b" : code === 12 ? "\\f" : code < 32 || code > 126 ? "\\u" + code.toString(16).padStart(4, "0") : unit[i];
      }
    }
    return out + '"';
  }
  if (typeof value === "number" || typeof value === "bigint") return String(value);
  if (typeof value === "boolean") return value ? "true" : "false";
  if (value === null) return "null";
  if (Array.isArray(value)) return "[" + value.map((item) => pyJson(item, separators)).join(separators[0]) + "]";
  throw new TypeError("unsupported value for pyJson");
}

async function sha256Hex(...parts) {
  const digest = new Uint8Array(await crypto.subtle.digest("SHA-256", concat(...parts)));
  return Array.from(digest, (byte) => byte.toString(16).padStart(2, "0")).join("");
}
const utf8 = (text) => new TextEncoder().encode(text);
const sortedNumbers = (set) => [...set].sort((a, b) => a - b);

export function importToken(compiledData, snapshotPayload, serial, generation, tail, protectedSegments) {
  return sha256Hex(compiledData, snapshotPayload, utf8(pyJson([serial, generation, tail, sortedNumbers(protectedSegments)], [",", ":"])));
}

export function offlineMergeToken(snapshot, serial, upper, lower) {
  return sha256Hex(utf8(pyJson([serial, upper, lower, snapshot.highwater])), snapshot.payload);
}

export function mergeToken(snapshot, result, serial, answer, upperId, lowerId) {
  const parts = [utf8(pyJson([serial, answer, upperId, lowerId, snapshot.highwater], [",", ":"])), snapshot.payload];
  for (const [segment, block] of [...result.segments].sort((a, b) => a[0] - b[0])) {
    const header = new Uint8Array(8);
    view(header).setUint32(0, segment, true);
    view(header).setUint32(4, block.length, true);
    parts.push(header, block);
  }
  return sha256Hex(...parts);
}
