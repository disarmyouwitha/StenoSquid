// Manager milestone M2: replace pinned compiled bases (when the pinned dictionary sources change), ported from
// plan_replacements in tools/storage/native_base_update.py. Everything except the selected compiled extents and source
// digests is kept; local journal edits that the new base now translates differently are reported as conflicts.
// `compiled` is a Map name -> {contents, pointers, digest}; readOriginal(address, length) may be async and is only
// needed to detect conflicts. Resolves to {update|null, changed, conflicts}.
import {crc32} from "./transfer_frame.js";
import {NoSpace} from "./plan.js";
import {SEGMENT, rowName} from "./snapshot.js";
import {packExtent} from "./snapshot_plan.js";
import {XIP, formatStroke} from "./decode.js";
import {reader} from "./lookup.js";

const view = (bytes) => new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
const concat = (...parts) => {
  const out = new Uint8Array(parts.reduce((sum, part) => sum + part.length, 0));
  let at = 0;
  for (const part of parts) { out.set(part, at); at += part.length; }
  return out;
};
const equalBytes = (a, b) => a.length === b.length && a.every((value, i) => value === b[i]);

function relocate(image, address) {
  const blob = Uint8Array.from(image.contents);
  const v = view(blob);
  for (const pointer of image.pointers) {
    const offset = v.getUint32(pointer, true);
    if (!(offset >= 0 && offset < blob.length)) throw new Error("Compiler emitted an out-of-bounds dictionary pointer");
    v.setUint32(pointer, address + offset, true);
  }
  return blob;
}

export async function planReplacements(snapshot, protectedSegments, compiled, readOriginal = null) {
  const rows = snapshot.rows.map((row) => Uint8Array.from(row));
  const extents = snapshot.extents.map((extent) => [...extent]);
  const free = new Set();
  for (let segment = 2; segment < snapshot.poolEnd; segment++) if (!protectedSegments.has(segment)) free.add(segment);
  const blocks = new Map(), changed = [], conflicts = [];

  const allocate = (count) => {
    if (free.size >= count + 3) {
      for (const segment of [...free].sort((a, b) => a - b)) {
        let run = true;
        for (let i = 0; i < count; i++) if (!free.has(segment + i)) { run = false; break; }
        if (run) {
          for (let i = 0; i < count; i++) free.delete(segment + i);
          return segment;
        }
      }
    }
    throw new NoSpace("The replacement images cannot fit beside the current store. No dictionaries changed.");
  };

  const overlays = new Map();
  for (const entry of (snapshot.entries ?? new Map()).values()) {
    if (!overlays.has(entry.identity)) overlays.set(entry.identity, []);
    overlays.get(entry.identity).push(entry);
  }

  const planned = new Set(), usedBases = new Set();
  const decoder = new TextDecoder("utf-8", {fatal: true});
  for (const row of rows) {
    const base = view(row).getUint16(8, true), member = row[10];
    if (base === 65535) continue;
    if (member || usedBases.has(base)) throw new Error("Expected individual dictionary images");
    usedBases.add(base);
    const name = rowName(row);
    const image = compiled.get(name);
    if (image === undefined) continue;
    planned.add(name);
    if (equalBytes(row.subarray(64, 96), image.digest)) continue;
    const count = Math.floor((image.contents.length + SEGMENT - 1) / SEGMENT);
    const segment = allocate(count);
    const blob = relocate(image, XIP + segment * SEGMENT);
    const edits = overlays.get(view(row).getUint32(0, true)) ?? [];
    if (readOriginal && edits.length) {
      const [oldStart, , oldSize, crc] = extents[base];
      const oldAddress = oldStart * SEGMENT;
      const original = Uint8Array.from(await readOriginal(oldAddress, oldSize));
      if (crc32(original) !== crc) throw new Error("Installed base checksum failed. No dictionaries changed.");
      const lookupOld = reader(original, XIP + oldAddress);
      const lookupNew = reader(blob, XIP + segment * SEGMENT);
      for (const entry of edits) {
        const outline = entry.strokes.map(formatStroke).join("/");
        const before = lookupOld(outline), after = lookupNew(outline);
        if (before !== after) {
          conflicts.push({dictionary: name, outline, before, after, local: !entry.operation ? decoder.decode(entry.text) : null});
        }
      }
    }
    row.set(image.digest, 64);
    changed.push(name);
    extents[base] = [segment, count, blob.length, crc32(blob), 1, 0n];
    for (let i = 0; i < count; i++) blocks.set(segment + i, blob.slice(i * SEGMENT, (i + 1) * SEGMENT));
  }
  if (planned.size !== compiled.size || [...compiled.keys()].some((name) => !planned.has(name))) {
    throw new Error("Installed dictionaries differ from this version’s pinned manifest.");
  }
  if (!changed.length) return {update: null, changed, conflicts};
  const metadata = allocate(1);
  const payload = concat(snapshot.payload.subarray(0, 32), ...rows, ...extents.map((extent) => packExtent(...extent)));
  blocks.set(metadata, payload);
  return {update: {segments: blocks, metadata, payload, reclaimed: 0, changes: 0}, changed, conflicts};
}
