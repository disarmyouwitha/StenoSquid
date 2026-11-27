// Offline repacking of a verified recovery bundle, ported from tools/storage/native_repack.py (the planner and verifier: build_repack,
// build_merge_repack, validate_pair and what they call) and seed_entries from build_native_image.py. A repack rewrites the whole
// dictionary pool, so everything here is checked three ways before anything is erased: the packed image is rebuilt from the
// source bundle, every baseline is relocated back and compared byte for byte, and every dictionary's effective translations and
// the active lookup priority are compared before and after.
import {assertionError} from "./errors.js";
import {XIP, compactEntries, effectiveEntries} from "./decode.js";
import {validateCompactLayout} from "./layout.js";
import {parseOutline} from "./lookup.js";
import {compileSnapshot} from "./compile.js";
import {NoSpace} from "./plan.js";
import {crc32} from "./transfer_frame.js";
import {digest, capture, reconstruct, catalogHeader} from "./recovery.js";
import {Snapshot, SEGMENT, OUTLINE_FIRST, rowName, compareKeys, outlineKey, textKey} from "./snapshot.js";
import {CompactImage, inspectFree, mergeRows, mergedEntries, planChange, sha256Hex} from "./snapshot_plan.js";
import {writeJournalHeader, packExtent} from "./install.js";

export const RESERVE = 3;
const COMMIT = 0x434f4d54;
const EXTENT_KINDS = {1: "compact", 2: "journal", 3: "forward-index", 4: "reverse-index"};

const view = (bytes) => new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
const concat = (...parts) => {
  const out = new Uint8Array(parts.reduce((sum, part) => sum + part.length, 0));
  let at = 0;
  for (const part of parts) { out.set(part, at); at += part.length; }
  return out;
};
const equalBytes = (a, b) => a.length === b.length && a.every((byte, i) => byte === b[i]);
const isDict = (value) => value !== null && typeof value === "object" && !Array.isArray(value);
const isInt = (value) => typeof value === "number" && Number.isInteger(value);
const hasKey = (object, key) => Object.prototype.hasOwnProperty.call(object, key);
const popcount = (value) => { let n = 0; for (let v = value >>> 0; v; v &= v - 1) n++; return n; };
const rowIdentity = (row) => view(row).getUint32(0, true);
const rowBase = (row) => view(row).getUint16(8, true);
const byCodePoint = (a, b) => {
  const x = [...a], y = [...b];
  for (let i = 0; i < Math.min(x.length, y.length); i++) if (x[i] !== y[i]) return x[i].codePointAt(0) - y[i].codePointAt(0);
  return x.length - y.length;
};
const opKey = (identity, strokes) => `${identity}:${strokes.join(",")}`;
const asNumber = (value) => (typeof value === "bigint" ? Number(value) : value);

export const segmentCount = (size) => Math.floor((size + SEGMENT - 1) / SEGMENT);
export const overlayRecordSpan = (strokes, text) => (43 + 4 * strokes.length + text.length) & ~3;

// Adjust only absolute JSC4 fields emitted by the supported compiler. `pointers`, when given, receives the sorted field offsets.
export function relocateCompact(blob, oldBase, newBase, pointers = null) {
  const v = view(blob);
  if (blob.length < 40 || String.fromCharCode(...blob.subarray(0, 4)) !== "JSC4" || blob.subarray(6, 8).some(Boolean) || blob.subarray(16, 32).some(Boolean)) {
    throw new Error("Unsupported compact collection for offline relocation");
  }
  const count = v.getUint16(4, true);
  if (!(count >= 1 && count <= 64) || 36 + 4 * count > blob.length) throw new Error("Invalid compact collection member count");
  const output = Uint8Array.from(blob);
  const out = view(output);
  const seen = new Set();
  const pointer = (field, needed = 1, alignment = 1) => {
    if (field < 0 || field > blob.length - 4) throw new Error("Invalid compact pointer field");
    const at = v.getUint32(field, true) - oldBase;
    if (at < 0 || at % alignment || needed > blob.length - at) throw new Error("Compact pointer outside its extent");
    if (!seen.has(field)) { out.setUint32(field, newBase + at, true); seen.add(field); }
    return at;
  };
  const textBytes = v.getUint32(8, true);
  const text = pointer(12, textBytes + 4);
  for (let member = 0; member < count; member++) {
    const definition = pointer(36 + member * 4, 16, 4);
    const maximum = blob[definition + 1];
    if (!(maximum >= 1 && maximum <= 16) || blob.subarray(definition + 2, definition + 4).some(Boolean)) throw new Error("Unsupported compact dictionary definition");
    pointer(definition + 4);
    if (pointer(definition + 8, textBytes) !== text) throw new Error("Unsupported per-dictionary text block");
    const table = pointer(definition + 12, maximum * 12, 4);
    for (let index = 0; index < maximum; index++) {
      const field = table + index * 12;
      pointer(field + 4);
      pointer(field + 8, 1, 4);
    }
  }
  if (pointers !== null) pointers.push(...[...seen].sort((a, b) => a - b));
  return output;
}

// Walk the physical SHI1 directory and compare its one-stroke results to JSC4.
export function verifyHints(blob, base) {
  const startAddress = base - XIP;
  const read = (address, size) => blob.subarray(address - startAddress, address - startAddress + size);
  validateCompactLayout(read, startAddress, blob.length);
  const v = view(blob);
  const members = v.getUint32(blob.length - 4, true);
  const directory = blob.length - 12 - 8 * members;
  const textBytes = v.getUint32(8, true), textPointer = v.getUint32(12, true);
  const text = textPointer - base;
  let previousEnd = (text + textBytes + 7) & ~3;
  const decoder = new TextDecoder("utf-8", {fatal: true});
  for (let member = 0; member < members; member++) {
    const start = v.getUint32(directory + member * 8, true), count = v.getUint32(directory + member * 8 + 4, true);
    if (start !== previousEnd || start + count * 8 > directory) throw new Error("Invalid compact hint record boundaries");
    previousEnd = start + count * 8;
    const wanted = new Map();
    for (const [outline, translation] of compactEntries(blob, base, member, members)) {
      if (outline.includes("/")) continue;
      const [stroke] = parseOutline(outline);
      const existing = wanted.get(translation);
      if (stroke && (existing === undefined || popcount(stroke) < popcount(existing) || (popcount(stroke) === popcount(existing) && stroke < existing))) wanted.set(translation, stroke);
    }
    const actual = new Map();
    let lastOffset = -1;
    for (let at = start; at < previousEnd; at += 8) {
      const offset = v.getUint32(at, true), stroke = v.getUint32(at + 4, true);
      if (!(lastOffset < offset && offset < textBytes)) throw new Error("Invalid or unsorted compact hint text pointer");
      lastOffset = offset;
      const end = blob.indexOf(0, text + offset);
      if (end < 0 || end >= text + textBytes) throw new Error("subsection not found");
      const translation = decoder.decode(blob.subarray(text + offset, end));
      if (actual.has(translation)) throw new Error("Duplicate compact hint");
      actual.set(translation, stroke);
    }
    if (actual.size !== wanted.size || [...actual].some(([translation, stroke]) => wanted.get(translation) !== stroke)) {
      throw new Error("Compact one-stroke hints differ from their forward dictionary");
    }
  }
  if (previousEnd !== directory) throw new Error("Unexpected data before the compact hint directory");
}

// A snapshot's shared overlay as a Map of key -> {identity, strokes, operation, text}, in key order.
export function overlayOperations(snapshot) {
  const records = [...snapshot.entries.values()].sort((a, b) => compareKeys([a.identity, a.strokes], [b.identity, b.strokes]));
  return new Map(records.map((record) => [opKey(record.identity, record.strokes), {identity: record.identity, strokes: record.strokes,
    operation: record.operation, text: record.text}]));
}

// The stock builder's sequential packing and its two indexes: [journal segments, forward index segments, reverse index segments].
export function overlayGeometry(operations) {
  let journals = 1, consumed = 256, sets = 0;
  for (const {strokes, text, operation} of operations.values()) {
    const span = overlayRecordSpan(strokes, text);
    if (consumed + span > SEGMENT) { journals++; consumed = 256; }
    consumed += span;
    if (operation === 0) sets++;
  }
  return [journals, segmentCount(4 * operations.size), segmentCount(4 * sets)];
}
const sum = (values) => values.reduce((total, value) => total + value, 0);

export function layoutStats(snapshot) {
  const owned = new Set([0, 1, snapshot.poolEnd]);
  const regions = [];
  for (const [start, count, size, , kind] of snapshot.extents) {
    for (let segment = start; segment < start + count; segment++) owned.add(segment);
    if (!(kind in EXTENT_KINDS)) throw Object.assign(new Error(`${kind}`), {name: "KeyError"});
    regions.push({start, segments: count, bytes: size, kind: EXTENT_KINDS[kind]});
  }
  const [free, run] = inspectFree(owned, snapshot.poolEnd);
  return {used_segments: snapshot.poolEnd - free, free_segments: free, largest_free_run: run, free_after_reserve: Math.max(0, free - RESERVE), regions};
}

const operationsAfter = (operations, converted) => new Map([...operations].filter(([, value]) => !converted.has(value.identity) || value.operation !== 0));

const plainError = (error) => error?.name === "Error";

export function chooseConversions(source, operations, compactSegments, names) {
  let current = compactSegments + sum(overlayGeometry(operations));
  const converted = new Map(), refusals = {};
  const eligible = new Map(source.rows.filter((row) => row[8] === 0xff && row[9] === 0xff && !row[11]).map((row) => [rowName(row), row]));
  const ordered = names === null ? [...eligible.keys()] : [...new Set(names)];
  for (const name of ordered) {
    const row = eligible.get(name);
    if (row === undefined) { refusals[name] = "Only journal-only user dictionaries can be converted"; continue; }
    const identity = rowIdentity(row);
    const entries = [...operations.values()].filter((record) => record.identity === identity && !record.operation).map((record) => [record.strokes, record.text]);
    if (!entries.length) { refusals[name] = "No live translations to compact"; continue; }
    let compiled;
    try {
      compiled = compileSnapshot(name, entries);
    } catch (error) {
      if (!plainError(error)) throw error;
      refusals[name] = error.message;
      continue;
    }
    const extra = segmentCount(compiled.data.length);
    const nextOperations = operationsAfter(operations, new Set([...converted.keys(), identity]));
    const prospective = compactSegments + extra + sum(overlayGeometry(nextOperations));
    if (prospective >= current) { refusals[name] = "No packed segment savings after retaining tombstones and other overlays"; continue; }
    current = prospective;
    compactSegments += extra;
    converted.set(identity, compiled);
  }
  return [converted, refusals, current];
}

// build_native_image.seed_entries: write the overlay journal records and both indexes. `entries` is a Map in key order.
export function seedEntries(image, extents, journalSegment, nextSegment, entries) {
  if (!entries.size) return nextSegment;
  const poolEnd = Math.floor(image.length / SEGMENT);
  let epoch = 1, segment = journalSegment, address = segment * SEGMENT + 256, sequence = 0;
  const records = new Map();
  for (const entry of entries.values()) {
    sequence++;
    const {identity, strokes, operation, text} = entry;
    const payload = new Uint8Array(8 + 4 * strokes.length + text.length);
    const p = view(payload);
    p.setUint32(0, identity, true); payload[4] = operation; payload[5] = strokes.length; p.setUint16(6, text.length, true);
    strokes.forEach((stroke, i) => p.setUint32(8 + 4 * i, stroke, true));
    payload.set(text, 8 + 4 * strokes.length);
    const span = overlayRecordSpan(strokes, text);
    if (address + span > (segment + 1) * SEGMENT) {
      segment = nextSegment; nextSegment++;
      if (nextSegment > poolEnd - 3) throw new Error("Saved dictionary entries exhaust the three-segment reserve");
      epoch++;
      extents.push(writeJournalHeader(image, segment, epoch));
      address = segment * SEGMENT + 256;
    }
    const sequenceBytes = new Uint8Array(8);
    view(sequenceBytes).setBigUint64(0, BigInt(sequence), true);
    const header = new Uint8Array(24);
    header.set([0x53, 0x51, 0x4a, 0x52]);
    const h = view(header);
    h.setUint32(4, span, true); h.setBigUint64(8, BigInt(sequence), true); h.setUint32(16, payload.length, true);
    h.setUint32(20, crc32(payload, crc32(sequenceBytes)), true);
    const tail = new Uint8Array(8);
    view(tail).setUint32(0, crc32(header), true); view(tail).setUint32(4, COMMIT, true);
    image.set(concat(header, tail, payload), address);
    records.set(opKey(identity, strokes), address);
    address += span;
  }
  const all = [...entries.values()];
  const reverse = all.filter((entry) => !entry.operation).sort((a, b) => compareKeys(textKey(a), textKey(b)));
  const forward = [...all].sort((a, b) => compareKeys(outlineKey(a), outlineKey(b)));
  for (const [kind, order] of [[3, forward], [4, reverse]]) {
    if (!order.length) continue;
    const index = new Uint8Array(4 * order.length);
    order.forEach((entry, i) => view(index).setUint32(4 * i, records.get(opKey(entry.identity, entry.strokes)), true));
    const count = Math.floor((index.length + SEGMENT - 1) / SEGMENT);
    if (nextSegment + count > poolEnd - 3) throw new Error("Saved dictionary indexes exhaust the three-segment reserve");
    image.set(index, nextSegment * SEGMENT);
    extents.push(packExtent(nextSegment, count, index.length, crc32(index), kind, kind === 3 ? OUTLINE_FIRST : 0));
    nextSegment += count;
  }
  return nextSegment;
}

function mergeLayout(source, merge) {
  if (merge === null) return [source.rows, new Set()];
  const [upper, lower] = mergeRows(source, merge[0], merge[1]);
  const omitted = new Set([upper, lower].map(rowBase));
  if (source.rows.some((row) => row !== upper && row !== lower && omitted.has(rowBase(row)))) throw new Error("A merge source shares its compact image.");
  return [source.rows.filter((row) => row !== lower), omitted];
}

export async function packLayout(sourceImage, source, operations, converted, expectedSegments, {merge = null} = {}) {
  if (expectedSegments + 2 > source.poolEnd - RESERVE) throw new Error("The packed target cannot retain the three-segment maintenance reserve");
  const [rowsToPack, omittedExtents] = mergeLayout(source, merge);
  const image = new Uint8Array(source.poolEnd * SEGMENT).fill(0xff);
  const extents = [];
  const remap = new Map(), newBases = new Map();
  let nextSegment = 2;
  const place = (data) => {
    const count = segmentCount(data.length);
    image.set(data, nextSegment * SEGMENT);
    extents.push(packExtent(nextSegment, count, data.length, crc32(data), 1, 0));
    nextSegment += count;
    return extents.length - 1;
  };
  // Factory dictionaries go in factory order even after an update has moved their extents or the user reordered them.
  const factory = new Map();
  for (const row of source.rows) {
    const identity = rowIdentity(row), base = rowBase(row);
    if (identity >= 2 && identity <= 7 && base !== 65535) factory.set(base, Math.min(identity, factory.get(base) ?? identity));
  }
  const compactExtents = source.extents.map((extent, index) => [extent, index]).filter(([extent, index]) => extent[4] === 1 && !omittedExtents.has(index))
    .map(([, index]) => index).sort((a, b) => ((factory.get(a) ?? 8) - (factory.get(b) ?? 8)) || (a - b));
  let journalSegment = null;
  for (const index of compactExtents) {
    if (!factory.has(index) && journalSegment === null) {
      journalSegment = nextSegment;
      extents.push(writeJournalHeader(image, journalSegment, 1));
      nextSegment++;
    }
    const [start, , size] = source.extents[index];
    const address = start * SEGMENT;
    const newAddress = nextSegment * SEGMENT;
    const blob = sourceImage.subarray(address, address + size);
    verifyHints(blob, XIP + address);
    const rebased = relocateCompact(blob, XIP + address, XIP + newAddress);
    if (!equalBytes(relocateCompact(rebased, XIP + newAddress, XIP + address), blob)) throw new Error("Compact relocation changed bytes outside pointer fields");
    remap.set(index, place(rebased));
  }
  if (journalSegment === null) {
    journalSegment = nextSegment;
    extents.push(writeJournalHeader(image, journalSegment, 1));
    nextSegment++;
  }
  for (const [identity, compiled] of converted) newBases.set(identity, place(compiled.at(XIP + nextSegment * SEGMENT)));

  const rows = [];
  for (const original of rowsToPack) {
    const row = Uint8Array.from(original);
    const identity = rowIdentity(row), base = rowBase(row);
    if (newBases.has(identity)) { view(row).setUint16(8, newBases.get(identity), true); row[10] = 0; row[11] = 1; }
    else if (base !== 65535) {
      if (!remap.has(base)) throw Object.assign(new Error(`${base}`), {name: "KeyError"});
      view(row).setUint16(8, remap.get(base), true);
    }
    rows.push(row);
  }
  const after = operationsAfter(operations, converted);
  const end = seedEntries(image, extents, journalSegment, nextSegment, after);
  if (end !== 2 + expectedSegments) throw new Error("Packed estimate differs from the generated image");
  const prefix = new Uint8Array(32);
  view(prefix).setUint32(0, source.nextId, true); view(prefix).setUint16(4, rows.length, true);
  view(prefix).setUint16(6, extents.length, true); view(prefix).setBigUint64(8, BigInt(after.size), true);
  const payload = concat(prefix, ...rows, ...extents);
  for (let root = 0; root < 2; root++) image.set(concat(catalogHeader(root + 1, payload), payload), root * SEGMENT);
  const snapshot = await Snapshot.load((address, n) => image.subarray(address, address + n), 32, payload.length, crc32(payload), {strict: true, poolEnd: source.poolEnd});
  return [image, snapshot];
}

const stripped = (row) => concat(row.subarray(0, 8), row.subarray(12));
const sameOperations = (a, b) => a.size === b.size && [...a].every(([key, value]) => {
  const other = b.get(key);
  return other !== undefined && other.operation === value.operation && equalBytes(other.text, value.text);
});
const sameEntries = (a, b) => a.size === b.size && [...a].every(([key, value]) => b.get(key) === value);

export async function verifyEffective(sourceImage, source, targetImage, target, expectedOperations, converted, merge) {
  if (source.nextId !== target.nextId || source.personal !== target.personal) throw new Error("Packed target changed a catalog identity counter");
  const [rows] = mergeLayout(source, merge);
  if (rows.length !== target.rows.length || rows.some((row, i) => !equalBytes(stripped(row), stripped(target.rows[i])))) {
    throw new Error("Packed target changed priority, flags, names or pinned provenance");
  }
  rows.forEach((before, i) => {
    const wanted = converted.has(rowIdentity(before)) ? Uint8Array.of(0, 1) : before.subarray(10, 12);
    if (!equalBytes(target.rows[i].subarray(10, 12), wanted)) throw new Error("Packed target changed an unrelated dictionary representation");
  });
  if (!sameOperations(overlayOperations(target), expectedOperations)) throw new Error("Packed target changed shared edits or tombstones");

  let lowerEntries = new Map();
  if (merge) {
    const [, lower] = mergeRows(source, merge[0], merge[1]);
    lowerEntries = await effectiveEntries(source, rowName(lower), async (at, n) => sourceImage.subarray(at, at + n));
  }
  const sourceWinners = new Map(), targetWinners = new Map(), perDictionary = {};
  for (const row of rows) {
    const identity = rowIdentity(row), name = rowName(row);
    let old = await effectiveEntries(source, name, async (at, n) => sourceImage.subarray(at, at + n));
    if (merge && identity === merge[0]) old = new Map([...lowerEntries, ...old]);
    const fresh = await effectiveEntries(target, name, async (at, n) => targetImage.subarray(at, at + n));
    if (!sameEntries(old, fresh)) throw new Error(`Packed target changed effective translations in ${name}`);
    const sorted = [...fresh].sort((a, b) => byCodePoint(a[0], b[0]) || byCodePoint(a[1], b[1]));
    perDictionary[name] = {entries: fresh.size, export_sha256: await sha256Hex(new TextEncoder().encode(jsonPairs(sorted)))};
    if (row[4]) {
      for (const [outline, text] of old) if (!sourceWinners.has(outline)) sourceWinners.set(outline, [name, text]);
      for (const [outline, text] of fresh) if (!targetWinners.has(outline)) targetWinners.set(outline, [name, text]);
    }
  }
  if (sourceWinners.size !== targetWinners.size || [...sourceWinners].some(([outline, [name, text]]) => {
    const other = targetWinners.get(outline);
    return !other || other[0] !== name || other[1] !== text;
  })) throw new Error("Packed target changed effective priority lookup");
  return {per_dictionary_entries: sum(Object.values(perDictionary).map((row) => row.entries)), dictionaries: perDictionary,
    active_lookup_winners: targetWinners.size, retained_overlay_records: expectedOperations.size};
}

// json.dumps(list of [outline, text] pairs, ensure_ascii=False, separators=(",", ":"))
function jsonString(text) {
  let out = '"';
  for (const ch of text) {
    const code = ch.codePointAt(0);
    out += ch === '"' ? '\\"' : ch === "\\" ? "\\\\" : ch === "\n" ? "\\n" : ch === "\r" ? "\\r" : ch === "\t" ? "\\t"
      : ch === "\b" ? "\\b" : ch === "\f" ? "\\f" : code < 32 ? "\\u" + code.toString(16).padStart(4, "0") : ch;
  }
  return out + '"';
}
const jsonPairs = (pairs) => "[" + pairs.map(([a, b]) => `[${jsonString(a)},${jsonString(b)}]`).join(",") + "]";

// Existing baselines and SHI1 survive verbatim after normalizing pointers.
export function verifyRelocated(sourceImage, source, targetImage, target, converted, merge) {
  const checked = new Set();
  const [rows] = mergeLayout(source, merge);
  if (rows.length !== target.rows.length) throw new Error("zip() argument 2 is shorter than argument 1");
  rows.forEach((before, i) => {
    const after = target.rows[i];
    const identity = rowIdentity(before), oldBase = rowBase(before), newBase = rowBase(after);
    if (newBase === 65535) {
      if (oldBase !== 65535) throw new Error("Packed target dropped a compact baseline");
      return;
    }
    if ((oldBase === 65535) !== converted.has(identity)) throw new Error("Unexpected baseline conversion");
    const [start, , size, , kind] = target.extents[newBase];
    if (kind !== 1) throw new Error("Packed target is missing a compact extent");
    const address = start * SEGMENT;
    const blob = targetImage.subarray(address, address + size);
    if (!checked.has(newBase)) { verifyHints(blob, XIP + address); checked.add(newBase); }
    if (oldBase !== 65535 && (merge === null || identity !== merge[0])) {
      const [oldStart, , oldSize] = source.extents[oldBase];
      const oldAddress = oldStart * SEGMENT;
      if (!equalBytes(relocateCompact(blob, XIP + address, XIP + oldAddress), sourceImage.subarray(oldAddress, oldAddress + oldSize))) {
        throw new Error("Packed target changed an existing compact baseline");
      }
    }
  });
}

// Run the production allocator on each installed user snapshot at its current size.
export function consolidationGeometry(image, snapshot) {
  const protectedSegments = new Set([0, 1, snapshot.poolEnd]);
  for (const [start, span] of snapshot.extents) for (let segment = start; segment < start + span; segment++) protectedSegments.add(segment);
  const result = {};
  for (const row of snapshot.rows) {
    if (row[11] !== 1) continue;
    const identity = rowIdentity(row), base = rowBase(row);
    const [start, span, size] = snapshot.extents[base];
    const address = start * SEGMENT;
    const pointers = [];
    const data = relocateCompact(image.subarray(address, address + size), XIP + address, 0, pointers);
    const name = rowName(row);
    try {
      const update = planChange(snapshot, protectedSegments, identity, new CompactImage(data, pointers), {consolidate: true});
      result[name] = {fits: true, image_segments: span, staging_segments: update.segments.size};
    } catch (error) {
      if (!(error instanceof NoSpace)) throw error;
      result[name] = {fits: false, image_segments: span, reason: error.message};
    }
  }
  return result;
}

export async function validatePair(sourceBackup, targetBackup, original, repacked) {
  const {image: sourceImage, snapshot: source} = original;
  const {image: targetImage, snapshot: target} = repacked;
  const metadata = hasKey(targetBackup, "repack") ? targetBackup.repack : null;
  const imageHash = await sha256Hex(targetImage);
  const sourceHash = sourceBackup.sha256;
  if (!isDict(metadata) || metadata.source_bundle_sha256 !== sourceHash || metadata.target_image_sha256 !== imageHash
      || sourceBackup.host_layout !== targetBackup.host_layout || source.poolEnd !== target.poolEnd) {
    throw new Error("Optimized bundle does not match the source, target image or host layout");
  }
  const names = hasKey(metadata, "converted") ? metadata.converted : null;
  if (!Array.isArray(names) || names.some((name) => typeof name !== "string") || names.length !== new Set(names).size) throw new Error("Invalid optimized bundle conversion list");
  const candidates = new Map(source.rows.filter((row) => row[8] === 0xff && row[9] === 0xff && !row[11]).map((row) => [rowName(row), rowIdentity(row)]));
  if (names.some((name) => !candidates.has(name))) throw new Error("Optimized bundle converted an ineligible dictionary");
  const converted = new Set(names.map((name) => candidates.get(name)));
  const merge = hasKey(metadata, "merged") ? metadata.merged : null;
  let operations = overlayOperations(source);
  if (hasKey(metadata, "merged")) {
    if (names.length || !Array.isArray(merge) || merge.length !== 2 || merge.some((identity) => !isInt(identity) || !(identity > 0 && identity <= 0xffffffff))) {
      throw new Error("Invalid optimized bundle merge pair");
    }
    mergeLayout(source, merge);
    operations = new Map([...operations].filter(([, value]) => !merge.includes(value.identity)));
  }
  verifyRelocated(sourceImage, source, targetImage, target, converted, merge);
  const verification = await verifyEffective(sourceImage, source, targetImage, target, operationsAfter(operations, converted), converted, merge);
  const before = layoutStats(source), after = layoutStats(target);
  if (after.free_segments < RESERVE) throw new Error("Optimized bundle does not retain the maintenance reserve");
  const saved = before.used_segments - after.used_segments;
  const gained = after.largest_free_run - before.largest_free_run;
  const recommendation = saved > 0 ? "space-savings" : gained > 0 && saved === 0 ? "contiguous-space-only" : "no-operation";
  return {format: "stenosquid-offline-repack-1", source_bundle_sha256: sourceHash, target_bundle_sha256: targetBackup.sha256, target_image_sha256: imageHash,
    source_capture: hasKey(sourceBackup, "capture") ? sourceBackup.capture : null, source_journal_highwater: asNumber(source.highwater),
    host_layout: sourceBackup.host_layout, maintenance_reserve: RESERVE, source_layout: before, target_layout: after, saved_segments: saved,
    source_layout_scope: "selected root in backup; excludes extents held only by the alternate root", largest_free_run_gain: gained,
    recommendation, converted: names, target_same_size_consolidation: consolidationGeometry(targetImage, target), verification};
}

// Returns [target image, bundle, report]. `backup` is a recovery bundle; `names` limits which journal dictionaries to convert.
export async function buildRepack(backup, names = null) {
  const original = await reconstruct(backup);
  const {image: sourceImage, snapshot: source} = original;
  const operations = overlayOperations(source);
  const compactSegments = sum(source.extents.filter((extent) => extent[4] === 1).map((extent) => segmentCount(extent[2])));
  const [converted, refused, expected] = chooseConversions(source, operations, compactSegments, names);
  const [targetImage, target] = await packLayout(sourceImage, source, operations, converted, expected);
  const bundle = await capture((address, n) => targetImage.subarray(address, address + n), target, backup.host_layout);
  const convertedNames = source.rows.filter((row) => converted.has(rowIdentity(row))).map(rowName);
  bundle.repack = {source_bundle_sha256: backup.sha256, target_image_sha256: await sha256Hex(targetImage), converted: convertedNames};
  bundle.sha256 = await digest(bundle);
  const report = await validatePair(backup, bundle, original, {image: targetImage, snapshot: target});
  return [targetImage, bundle, {...report, refused}];
}

export async function buildMergeRepack(backup, original, upperId, lowerId) {
  const {image: sourceImage, snapshot: source} = original;
  const merge = [upperId, lowerId];
  const [upper, lower] = mergeRows(source, upperId, lowerId);
  const {merged} = await mergedEntries(source, upper, lower, async (address, size) => sourceImage.subarray(address, address + size));
  const unique = new Map();
  for (const [outline, text] of merged) { const strokes = parseOutline(outline); unique.set(strokes.join(","), [strokes, new TextEncoder().encode(text)]); }
  const compiled = compileSnapshot(rowName(upper), [...unique.values()]);
  const [, omitted] = mergeLayout(source, merge);
  const operations = new Map([...overlayOperations(source)].filter(([, value]) => !merge.includes(value.identity)));
  let segments = sum(source.extents.map((extent, index) => [extent, index]).filter(([extent, index]) => extent[4] === 1 && !omitted.has(index))
    .map(([extent]) => segmentCount(extent[2])));
  segments += segmentCount(compiled.data.length) + sum(overlayGeometry(operations));
  const [targetImage, target] = await packLayout(sourceImage, source, operations, new Map([[upperId, compiled]]), segments, {merge});
  const bundle = await capture((address, n) => targetImage.subarray(address, address + n), target, backup.host_layout);
  bundle.repack = {source_bundle_sha256: backup.sha256, target_image_sha256: await sha256Hex(targetImage), converted: [], merged: merge};
  bundle.sha256 = await digest(bundle);
  await validatePair(backup, bundle, original, {image: targetImage, snapshot: target});
  return [bundle, rowName(upper)];
}
