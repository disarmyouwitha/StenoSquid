// Manager milestone M1: the catalog and journal reader, ported from tools/storage/native_update.py.
// Same checks, in the same order, with the same error messages; the Python code is the oracle and
// tools/manager/browser/test_snapshot.mjs compares this port with golden vectors that Python generated
// (tools/manager/browser/golden_snapshot.py). The flash reader may be synchronous or async, so the same
// code can read a keyboard over USB or a captured image.
import {crc32} from "./transfer_frame.js";

export const SEGMENT = 65536;
export const OUTLINE_FIRST = 1;
export const SMALL_POOL_END = 255;
export const LARGE_POOL_END = 1023;
export const COMMIT = 0x434f4d54;

const ASCII = (text) => Uint8Array.from(text, (c) => c.charCodeAt(0));
const view = (bytes) => new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
const equal = (a, b) => a.length === b.length && a.every((value, i) => value === b[i]);
const concat = (...parts) => {
  const out = new Uint8Array(parts.reduce((sum, part) => sum + part.length, 0));
  let at = 0;
  for (const part of parts) { out.set(part, at); at += part.length; }
  return out;
};
const text8 = (bytes) => new TextDecoder("utf-8", {fatal: true}).decode(bytes);

export class Edit {
  constructor(address, identity, operation, strokes, text) {
    Object.assign(this, {address, identity, operation, strokes, text});
  }
  get key() { return `${this.identity}:${this.strokes.join(",")}`; }
  get span() { return (43 + 4 * this.strokes.length + this.text.length) & ~3; }
}

// Returns {edit, sequence (BigInt)}.
export function readRecord(data, offset, start) {
  const head = view(data.subarray(offset, offset + 32));
  const magic = data.subarray(offset, offset + 4);
  const span = head.getUint32(4, true), sequence = head.getBigUint64(8, true), size = head.getUint32(16, true);
  const crc = head.getUint32(20, true), headerCrc = head.getUint32(24, true), commit = head.getUint32(28, true);
  if (span !== ((35 + size) & ~3) || offset + span > SEGMENT || size < 12 || !sequence) {
    throw new Error("Invalid staged-source record span");
  }
  const body = data.subarray(offset + 32, offset + 32 + size);
  const seed = new Uint8Array(8);
  view(seed).setBigUint64(0, sequence, true);
  if (!equal(magic, ASCII("SQJR")) || commit !== COMMIT || crc32(data.subarray(offset, offset + 24)) !== headerCrc
      || crc32(body, crc32(seed)) !== crc) {
    throw new Error("Invalid staged-source record checksum");
  }
  const key = view(body);
  const identity = key.getUint32(0, true), operation = body[4], count = body[5], length = key.getUint16(6, true);
  if (!identity || operation > 2 || !(count >= 1 && count <= 16) || length > 2047) {
    throw new Error("Invalid staged-source record key");
  }
  if (size !== 8 + 4 * count + length || (operation && length)) throw new Error("Invalid staged-source record length");
  const strokes = Array.from({length: count}, (_, i) => key.getUint32(8 + 4 * i, true));
  const text = body.slice(8 + 4 * count);
  if (strokes.some((stroke) => !(stroke > 0 && stroke < 2 ** 25)) || text.includes(0)) {
    throw new Error("Invalid staged-source translation");
  }
  text8(text);
  return {edit: new Edit(start + offset, identity, operation, strokes, text), sequence};
}

export function encodeRecord(edit, sequence) {
  const payload = new Uint8Array(8 + 4 * edit.strokes.length + edit.text.length);
  const body = view(payload);
  body.setUint32(0, edit.identity, true);
  payload[4] = edit.operation;
  payload[5] = edit.strokes.length;
  body.setUint16(6, edit.text.length, true);
  edit.strokes.forEach((stroke, i) => body.setUint32(8 + 4 * i, stroke, true));
  payload.set(edit.text, 8 + 4 * edit.strokes.length);
  const seed = new Uint8Array(8);
  view(seed).setBigUint64(0, BigInt(sequence), true);
  const header = new Uint8Array(24);
  header.set(ASCII("SQJR"));
  const h = view(header);
  h.setUint32(4, edit.span, true);
  h.setBigUint64(8, BigInt(sequence), true);
  h.setUint32(16, payload.length, true);
  h.setUint32(20, crc32(payload, crc32(seed)), true);
  const trailer = new Uint8Array(8);
  view(trailer).setUint32(0, crc32(header), true);
  view(trailer).setUint32(4, COMMIT, true);
  return concat(header, trailer, payload);
}

const rowName = (row) => text8(row.subarray(16, 16 + row[12]));

// `read(address, length)` returns the bytes (or a promise of them). Resolves to a Snapshot.
export class Snapshot {
  static async load(read, catalogAddress, catalogBytes, crc, {metadataOnly = false, strict = false, poolEnd = SMALL_POOL_END} = {}) {
    const self = new Snapshot();
    self.poolEnd = poolEnd;
    const payload = Uint8Array.from(await read(catalogAddress, catalogBytes));
    if (payload.length !== catalogBytes || crc32(payload) !== crc) throw new Error("Catalog changed or failed its checksum");
    self.payload = payload;
    const head = view(payload);
    self.nextId = head.getUint32(0, true);
    const count = head.getUint16(4, true), extents = head.getUint16(6, true), through = head.getBigUint64(8, true);
    const rowsEnd = 32 + count * 96;
    if (count > 64 || extents > 253 || payload.length !== rowsEnd + extents * 24) throw new Error("Invalid source catalog shape");
    self.rows = Array.from({length: count}, (_, i) => payload.slice(32 + i * 96, 32 + (i + 1) * 96));
    self.extents = Array.from({length: extents}, (_, i) => {
      const e = view(payload.subarray(rowsEnd + i * 24, rowsEnd + (i + 1) * 24));
      return [e.getUint16(0, true), e.getUint16(2, true), e.getUint32(4, true), e.getUint32(8, true), e.getUint32(12, true), e.getBigUint64(16, true)];
    });
    const identities = new Set(self.rows.map((row) => view(row).getUint32(0, true)));
    const personal = self.rows.filter((row) => equal(row.subarray(16, 16 + row[12]), ASCII("user_dictionary")));
    if (personal.length !== 1 || !(personal[0][8] === 0xff && personal[0][9] === 0xff) && personal[0][11] !== 1) {
      throw new Error("Expected one personal dictionary");
    }
    self.personal = view(personal[0]).getUint32(0, true);
    for (const [, , , , kind, auxiliary] of self.extents) {
      if (kind === 3 && auxiliary !== BigInt(OUTLINE_FIRST)) {
        throw new Error("Unsupported forward journal checkpoint. Reprovision with current firmware and Manager.");
      }
      if (kind === 4 && auxiliary) throw new Error("Unsupported reverse journal checkpoint");
    }
    if (metadataOnly) return self;
    self.journals = self.extents.filter((item) => item[4] === 2);
    self.highwater = 0n;
    self.anchor = null;
    self.used = new Map();
    const records = new Map(), tail = [], sequences = new Map();
    let epochs = 0n, boundary = !through;
    for (const [segment, , , , , epoch] of self.journals) {
      if (strict && epoch <= epochs) throw new Error("Recovery journal epochs are out of order");
      epochs = epoch;
      const start = segment * SEGMENT;
      const data = Uint8Array.from(await read(start, SEGMENT));
      const wanted = new Uint8Array(16);
      wanted.set(ASCII("SQJSEG01"));
      view(wanted).setBigUint64(8, epoch, true);
      if (data.length !== SEGMENT || !equal(data.subarray(0, 16), wanted)) throw new Error("Invalid source journal header");
      if (crc32(data.subarray(0, 16)) !== view(data).getUint32(16, true)) throw new Error("Invalid source journal checksum");
      let offset = 256;
      while (offset + 32 <= SEGMENT) {
        if (view(data).getUint32(offset + 28, true) !== COMMIT) break;
        const {edit, sequence} = readRecord(data, offset, start);
        if (sequence <= self.highwater) throw new Error("Source journal sequence is out of order");
        self.highwater = sequence;
        self.anchor = segment;
        if (sequence > through) tail.push(edit);
        records.set(edit.address, edit);
        if (strict) {
          sequences.set(edit.address, sequence);
          boundary ||= sequence === through;
        }
        offset += edit.span;
      }
      self.used.set(segment, offset - 256);
    }

    // The forward checkpoint is authoritative; replaying retained history could resurrect an old Set.
    self.entries = new Map();
    if (strict && !boundary) throw new Error("Recovery checkpoint boundary is missing");
    const indexed = new Map();
    for (const [start, , length, expected, kind] of self.extents) {
      if (kind !== 3 && (!strict || kind !== 4)) continue;
      const data = Uint8Array.from(await read(start * SEGMENT, length));
      if (data.length !== length || crc32(data) !== expected) throw new Error("Source forward index failed checksum");
      const index = view(data);
      let previous = null;
      for (let at = 0; at + 4 <= data.length; at += 4) {
        const address = index.getUint32(at, true);
        const entry = records.get(address);
        if (!entry || entry.operation === 2) throw new Error("Invalid source index record");
        if (strict) {
          const sequence = sequences.get(address);
          if (entry.identity >= self.nextId || sequence > through || (kind === 4 && entry.operation)) {
            throw new Error("Invalid recovery index record");
          }
          const key = kind === 3 ? outlineKey(entry) : textKey(entry);
          if (previous !== null && compareKeys(key, previous) <= 0) throw new Error("Recovery index is out of order");
          previous = key;
          if (!indexed.has(kind)) indexed.set(kind, new Set());
          indexed.get(kind).add(address);
        }
        if (kind === 3 && identities.has(entry.identity)) self.entries.set(entry.key, entry);
      }
    }
    if (strict) {
      const forward = new Set([...(indexed.get(3) ?? [])].filter((address) => !records.get(address).operation));
      const reverse = indexed.get(4) ?? new Set();
      if (forward.size !== reverse.size || [...forward].some((address) => !reverse.has(address))) {
        throw new Error("Recovery indexes disagree");
      }
    }
    for (const entry of tail) {
      if (!identities.has(entry.identity)) continue;
      if (entry.operation === 2) self.entries.delete(entry.key);
      else self.entries.set(entry.key, entry);
    }
    if (through > self.highwater || tail.length > 256) throw new Error("Source journal checkpoint is inconsistent");
    return self;
  }
}

// Python orders (strokes tuple, identity) and (text bytes, (identity, strokes tuple)) lexicographically.
const outlineKey = (entry) => [entry.strokes, entry.identity];
const textKey = (entry) => [Array.from(entry.text), [entry.identity, entry.strokes]];
function compareKeys(a, b) {
  if (Array.isArray(a) && Array.isArray(b)) {
    for (let i = 0; i < Math.min(a.length, b.length); i++) {
      const order = compareKeys(a[i], b[i]);
      if (order) return order;
    }
    return a.length - b.length;
  }
  return a < b ? -1 : a > b ? 1 : 0;
}

export function validateName(name) {
  if (!/^[A-Za-z0-9][A-Za-z0-9 ._-]{0,43}$/.test(name) || name.endsWith(" ")) {
    throw new Error("Use 1–44 letters, digits, spaces, dots, underscores or hyphens for the name");
  }
  return name;
}

const repr = (text) => (text.includes("'") && !text.includes('"') ? `"${text}"` : `'${text.replaceAll("'", "\\'")}'`);

// move = [name, position], rename = [name, label], delete = name. Returns the new catalog payload or null for no change.
export function metadataPayload(snapshot, {move = null, rename = null, delete: remove = null} = {}) {
  const rows = [...snapshot.rows];
  const names = rows.map(rowName);
  const target = move ? move[0] : rename ? rename[0] : remove;
  if (!names.includes(target)) throw new Error(`No installed dictionary named ${repr(target)}`);
  const index = names.indexOf(target);
  if (move) {
    const position = move[1] - 1;
    if (!(position >= 0 && position < rows.length)) throw new Error("Choose a position in the current priority list");
    if (index === position) return null;
    rows.splice(position, 0, rows.splice(index, 1)[0]);
  } else {
    const independent = rows[index][8] === 0xff && rows[index][9] === 0xff;
    if (target === "user_dictionary" || rows[index][11] === 2 || (!independent && !(rename && rows[index][11] === 1))) {
      throw new Error("Only independent named dictionaries can be renamed or deleted");
    }
    if (rename) {
      const label = validateName(rename[1]);
      if (label === target) return null;
      if (names.includes(label)) throw new Error(`A dictionary named ${repr(label)} is already installed`);
      const row = Uint8Array.from(rows[index]);
      row[12] = label.length;
      row.fill(0, 16, 60);
      row.set(ASCII(label), 16);
      rows[index] = row;
    } else {
      rows.splice(index, 1);
    }
  }
  const prefix = snapshot.payload.slice(0, 32);
  view(prefix).setUint16(4, rows.length, true);
  return concat(prefix, ...rows, snapshot.payload.subarray(32 + snapshot.rows.length * 96));
}
