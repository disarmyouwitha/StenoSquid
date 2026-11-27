// Manager: the recovery bundle (a complete backup) — capture, digest and offline reconstruction/validation, ported from
// tools/storage/native_recovery.py (digest, pack, unpack, descriptors, capture, validate_rows, reconstruct). The Python code
// is the oracle; golden_recovery.py records what it does with a valid backup and with hundreds of damaged variants.
import {crc32} from "./transfer_frame.js";
import {Snapshot, SEGMENT, OUTLINE_FIRST, SMALL_POOL_END, LARGE_POOL_END, rowName, validateName} from "./snapshot.js";
import {XIP, compactEntriesLazy, requireSnapshotHint} from "./decode.js";
import {reader} from "./lookup.js";
import {sha256Hex} from "./snapshot_plan.js";

export const FORMAT = "stenosquid-backup-2";
export const LAYOUTS = ["Mac U.S.", "Windows U.S. (plain text)"];
const POOL_ENDS = [SMALL_POOL_END, LARGE_POOL_END];
// The factory dictionaries that come from a pinned source image (build_native_image.SOURCES with a source file).
const FACTORY = new Map([[2, "Lapwing Commands"], [3, "Lapwing Numbers"], [4, "Lapwing UK Additions"], [5, "Lapwing Proper Nouns"],
  [6, "Lapwing Base"], [7, "Plover Base"]]);

const isDict = (value) => value !== null && typeof value === "object" && !Array.isArray(value);
const isInt = (value) => (typeof value === "number" && Number.isInteger(value)) || typeof value === "bigint";
const has = (object, key) => Object.prototype.hasOwnProperty.call(object, key);
const getDefault = (object, key, fallback = null) => (has(object, key) ? object[key] : fallback);
const byCodePoint = (a, b) => {
  const x = [...a], y = [...b];
  for (let i = 0; i < Math.min(x.length, y.length); i++) if (x[i] !== y[i]) return x[i].codePointAt(0) - y[i].codePointAt(0);
  return x.length - y.length;
};

// json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
export function dumps(value) {
  if (typeof value === "string") {
    // Recovery blobs are large ASCII strings. Quote them in one pass, then
    // match Python's ensure_ascii escaping for the remaining code units.
    return JSON.stringify(value).replace(/[\u007f-\uffff]/g, (ch) =>
      "\\u" + ch.charCodeAt(0).toString(16).padStart(4, "0"));
  }
  if (typeof value === "number" || typeof value === "bigint") return String(value);
  if (typeof value === "boolean") return value ? "true" : "false";
  if (value === null) return "null";
  if (Array.isArray(value)) return "[" + value.map(dumps).join(",") + "]";
  return "{" + Object.keys(value).sort(byCodePoint).map((key) => dumps(key) + ":" + dumps(value[key])).join(",") + "}";
}

export async function digest(backup) {
  const manifest = {};
  for (const key of Object.keys(backup)) if (key !== "sha256") manifest[key] = backup[key];
  return sha256Hex(new TextEncoder().encode(dumps(manifest)));
}

export function pack(data) {
  let text = "";
  for (let i = 0; i < data.length; i += 0x8000) text += String.fromCharCode(...data.subarray(i, i + 0x8000));
  return btoa(text);
}

export function unpack(encoded, expected = null) {
  if (typeof encoded !== "string" || encoded.length > Math.floor((LARGE_POOL_END * SEGMENT + 2) / 3) * 4) throw new Error("Invalid recovery data size");
  if (!/^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/.test(encoded)) throw new Error("Invalid base64 in a recovery bundle");
  const raw = atob(encoded);
  const data = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; i++) data[i] = raw.charCodeAt(i);
  if (expected !== null && data.length !== expected) throw new Error("Recovery data size differs from its catalog");
  return data;
}

// The catalog's extent descriptors: [start, span, size, crc, kind, epoch (BigInt)].
export function descriptors(payload, poolEnd = SMALL_POOL_END) {
  if (!(payload.length >= 32 && payload.length <= 32 + 64 * 96 + 253 * 24)) throw new Error("Invalid recovery catalog size");
  const view = new DataView(payload.buffer, payload.byteOffset, payload.byteLength);
  const nextId = view.getUint32(0, true), rows = view.getUint16(4, true), count = view.getUint16(6, true);
  const end = 32 + rows * 96;
  if (nextId < 8 || rows > 64 || count > 253 || payload.subarray(16, 32).some((byte) => byte) || payload.length !== end + count * 24) {
    throw new Error("Invalid recovery catalog shape");
  }
  const used = new Set([0, 1, poolEnd]), kinds = new Set(), result = [];
  for (let at = end; at < payload.length; at += 24) {
    const start = view.getUint16(at, true), span = view.getUint16(at + 2, true), size = view.getUint32(at + 4, true);
    const crc = view.getUint32(at + 8, true), kind = view.getUint32(at + 12, true), epoch = view.getBigUint64(at + 16, true);
    result.push([start, span, size, crc, kind, epoch]);
    if (kind === 3 && epoch !== BigInt(OUTLINE_FIRST)) throw new Error("Unsupported forward journal checkpoint in recovery bundle");
    const run = [];
    for (let segment = start; segment < start + span; segment++) run.push(segment);
    if (!(start >= 2 && start < poolEnd) || !(span >= 1 && span <= poolEnd - start) || !(size > 0 && size <= span * SEGMENT)
        || run.some((segment) => used.has(segment)) || ![1, 2, 3, 4].includes(kind)
        || (kind === 2 && (span !== 1 || size !== SEGMENT || !epoch || crc))
        || ((kind === 1 || kind === 4) && epoch)
        || ((kind === 3 || kind === 4) && (size % 4 || kinds.has(kind)))) {
      throw new Error("Invalid or overlapping recovery extent");
    }
    for (const segment of run) used.add(segment);
    kinds.add(kind);
  }
  if (!kinds.has(2)) throw new Error("Recovery catalog is missing its journal");
  return result;
}

// `read(address, size)` may be async. Returns the backup object (with its sha256) after validating it offline.
export async function capture(read, snapshot, layout) {
  const payload = snapshot.payload, poolEnd = snapshot.poolEnd;
  const backup = {format: FORMAT, host_layout: layout, catalog: pack(payload), extents: []};
  for (const [start, , size] of descriptors(payload, poolEnd)) backup.extents.push(pack(Uint8Array.from(await read(start * SEGMENT, size))));
  if (poolEnd !== SMALL_POOL_END) backup.pool_segments = poolEnd;
  backup.sha256 = await digest(backup);
  await reconstruct(backup);
  return backup;
}

const any = (bytes) => bytes.some((byte) => byte);

function validateRows(snapshot, image) {
  const references = new Map(), names = new Set(), ids = new Set(), counts = new Map();
  const factoryNames = new Set(FACTORY.values());
  for (const row of snapshot.rows) {
    const view = new DataView(row.buffer, row.byteOffset, row.byteLength);
    const identity = view.getUint32(0, true), enabled = view.getUint32(4, true), base = view.getUint16(8, true);
    const member = row[10], marker = row[11], nameBytes = row[12];
    const name = rowName(row);
    if (!(identity > 0 && identity < snapshot.nextId) || ids.has(identity) || names.has(name) || enabled > 1 || !(nameBytes >= 1 && nameBytes <= 44)
        || any(row.subarray(13, 16)) || any(row.subarray(16 + nameBytes, 64)) || marker > 2) {
      throw new Error("Invalid recovery dictionary row");
    }
    names.add(name);
    ids.add(identity);
    if (FACTORY.has(identity)) {
      if (name !== FACTORY.get(identity) || !any(row.subarray(64, 96))) throw new Error("Invalid pinned source identity");
      const absent = marker === 2 && base === 65535 && !enabled && !member;
      if (!absent && (marker || base === 65535)) throw new Error("Invalid factory image state");
    } else {
      if (name !== "user_dictionary") {
        if (identity < 8 || factoryNames.has(name)) throw new Error("Invalid user-owned dictionary identity");
        validateName(name);
      }
      if (marker > 1 || any(row.subarray(64, 96)) || Boolean(marker) !== (base !== 65535) || member) throw new Error("Invalid user-owned snapshot descriptor");
    }
    if (base === 65535) {
      if (member) throw new Error("Unexpected compiled member for a journal dictionary");
      continue;
    }
    if (base >= snapshot.extents.length || snapshot.extents[base][4] !== 1) throw new Error("Invalid compact image reference");
    if (!references.has(base)) references.set(base, new Set());
    const members = references.get(base);
    if (members.has(member)) throw new Error("Duplicate compiled image member");
    members.add(member);
    const [start, , size] = snapshot.extents[base];
    const address = start * SEGMENT;
    const blob = image.slice(address, address + size);
    if (blob.length < 40) throw new Error("Truncated compact image");
    const count = new DataView(blob.buffer).getUint16(4, true);
    if (count !== 1 || member) throw new Error("Invalid compact image member count");
    counts.set(base, count);
    if (marker) requireSnapshotHint(blob, XIP + address);
    // Walk the compiled data using the same reader as export.
    try {
      const lookup = reader(blob, XIP + address, member, count);
      const seen = new Set();
      for (const [outline, text] of compactEntriesLazy(blob, XIP + address, member, count)) {
        if (seen.has(outline) || lookup(outline) !== text) throw new Error("Compact recovery image failed lookup verification");
        seen.add(outline);
      }
    } catch (error) {
      // Python: except (AssertionError, IndexError, struct.error) -> ValueError. A ValueError (including its own) passes through.
      if (error?.name === "AssertionError" || error instanceof RangeError) throw new Error("Malformed compact recovery image");
      throw error;
    }
  }
  if ([...FACTORY.keys()].some((identity) => !ids.has(identity)) || !names.has("user_dictionary")) {
    throw new Error("Recovery bundle is missing a pinned or personal dictionary");
  }
  for (const [base, members] of references) {
    if (members.size !== counts.get(base) || [...members].some((member) => member >= counts.get(base))) throw new Error("Recovery bundle is missing a compact image member");
  }
  const compact = new Set();
  snapshot.extents.forEach((extent, index) => { if (extent[4] === 1) compact.add(index); });
  if (references.size !== compact.size || [...references.keys()].some((base) => !compact.has(base))) throw new Error("Unreferenced compact extent in recovery bundle");
}

export function catalogHeader(generation, payload) {
  const out = new Uint8Array(32);
  const view = new DataView(out.buffer);
  out.set(new TextEncoder().encode("SQBCAT02"));
  view.setBigUint64(8, BigInt(generation), true);
  view.setUint32(16, payload.length, true);
  view.setUint32(20, crc32(payload), true);
  view.setUint32(24, crc32(out.subarray(0, 24)), true);
  view.setUint32(28, 0x434f4d54, true);
  return out;
}

// Returns {image, snapshot}: coherent roots for offline validation only.
export async function reconstruct(backup) {
  if (!isDict(backup) || getDefault(backup, "format") !== FORMAT || !LAYOUTS.includes(getDefault(backup, "host_layout"))
      || getDefault(backup, "sha256") !== await digest(backup)) {
    throw new Error("Recovery bundle version, host layout or checksum is invalid");
  }
  if (has(backup, "capture")) {
    const source = backup.capture;
    if (!isDict(source) || typeof getDefault(source, "serial") !== "string" || !source.serial
        || !isInt(getDefault(source, "generation")) || source.generation < 1
        || typeof getDefault(source, "timestamp") !== "string" || !source.timestamp) {
      throw new Error("Invalid recovery capture identity");
    }
  }
  const poolEnd = getDefault(backup, "pool_segments", SMALL_POOL_END);
  if (typeof poolEnd !== "number" || !POOL_ENDS.includes(poolEnd)) throw new Error("Unsupported recovery flash geometry");
  const payload = unpack(getDefault(backup, "catalog"));
  const extents = descriptors(payload, poolEnd);
  const chunks = getDefault(backup, "extents");
  if (!Array.isArray(chunks) || chunks.length !== extents.length) throw new Error("Recovery bundle is missing an extent");
  const image = new Uint8Array(poolEnd * SEGMENT).fill(0xff);
  extents.forEach(([start, , size, crc, kind], index) => {
    const blob = unpack(chunks[index], size);
    if (kind !== 2 && crc32(blob) !== crc) throw new Error("Recovery extent checksum differs from its catalog");
    image.set(blob, start * SEGMENT);
  });
  for (let slot = 0; slot < 2; slot++) {
    image.set(catalogHeader(slot + 1, payload), slot * SEGMENT);
    image.set(payload, slot * SEGMENT + 32);
  }
  const snapshot = await Snapshot.load((address, size) => image.slice(address, address + size), 32, payload.length, crc32(payload), {strict: true, poolEnd});
  validateRows(snapshot, image);
  return {image, snapshot};
}
