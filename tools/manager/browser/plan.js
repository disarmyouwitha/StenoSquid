// Manager milestone M2: the copy-on-write update planner, ported from plan() in tools/storage/native_update.py.
// Stages replacements in free flash segments and returns the finished update (segments, catalog payload), or null when
// there is nothing to change. Same allocation order, same record and index layout, byte for byte (golden vectors from
// tools/manager/browser/golden_plan.py gate this; the Python code is the oracle).
import {crc32} from "./transfer_frame.js";
import {Edit, SEGMENT, OUTLINE_FIRST, encodeRecord, metadataPayload, validateName, rowName, outlineKey, textKey, compareKeys} from "./snapshot.js";

export class NoSpace extends Error {}

const ASCII = (text) => Uint8Array.from(text, (c) => c.charCodeAt(0));
const view = (bytes) => new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);

function packExtent(segment, span, bytes, crc, kind, auxiliary) {
  const out = new Uint8Array(24);
  const v = view(out);
  v.setUint16(0, segment, true); v.setUint16(2, span, true); v.setUint32(4, bytes, true);
  v.setUint32(8, crc, true); v.setUint32(12, kind, true); v.setBigUint64(16, BigInt(auxiliary), true);
  return out;
}

function journalHeader(block, epoch) {
  const header = new Uint8Array(32); // 8 magic + 8 epoch + 4 crc of those 16 bytes + 12 zero
  header.set(ASCII("SQJSEG01"));
  view(header).setBigUint64(8, epoch, true);
  view(header).setUint32(16, crc32(header.subarray(0, 16)), true);
  block.set(header, 0);
}

const bytesCompare = (a, b) => compareKeys(Array.from(a), Array.from(b));

// `incoming` is null (compact the personal journal) or an array of [strokes[], textBytes] (personal edits, or the
// contents of a new named dictionary when `name` is given). `protectedSegments` is a Set. move/rename/delete as in
// metadataPayload. Returns {segments: Map(segment -> Uint8Array), metadata, payload, reclaimed, changes} or null.
export function plan(snapshot, protectedSegments, incoming = null, {name = null, limit = 64, move = null, rename = null, delete: remove = null} = {}) {
  const free = new Set();
  for (let segment = 2; segment < snapshot.poolEnd; segment++) if (!protectedSegments.has(segment)) free.add(segment);
  const segments = new Map();

  const allocate = (count = 1) => {
    if (free.size - count >= 3) {
      for (const start of [...free].sort((a, b) => a - b)) {
        let contiguous = true;
        for (let i = 0; i < count; i++) if (!free.has(start + i)) { contiguous = false; break; }
        if (contiguous) {
          for (let i = 0; i < count; i++) free.delete(start + i);
          return start;
        }
      }
    }
    throw new NoSpace("Not enough contiguous staging space; no data was changed");
  };

  if (move || rename || remove) {
    const payload = metadataPayload(snapshot, {move, rename, delete: remove});
    if (payload === null) return null;
    const metadata = allocate();
    return {segments: new Map([[metadata, payload]]), metadata, payload, reclaimed: 0, changes: 0};
  }

  let identity = snapshot.personal, nextId = snapshot.nextId;
  if (name !== null) {
    validateName(name);
    if (snapshot.rows.some((row) => rowName(row) === name)) throw new Error(`A dictionary named '${name}' is already installed`);
    if (snapshot.rows.length >= limit || nextId === 0xffffffff) throw new Error("The keyboard has reached its dictionary limit");
    identity = nextId;
    nextId += 1;
  }

  const entries = new Map(snapshot.entries);
  let pending = [];
  const victims = new Set();
  if (incoming === null) {
    const liveBytes = new Map([...snapshot.used.keys()].map((segment) => [segment, 0]));
    for (const entry of entries.values()) {
      const segment = Math.floor(entry.address / SEGMENT);
      liveBytes.set(segment, liveBytes.get(segment) + entry.span);
    }
    const anchor = new Set([snapshot.anchor]);
    if (snapshot.journals.length) anchor.add(snapshot.journals.at(-1)[0]);
    const candidates = [...snapshot.used].filter(([segment, used]) => !anchor.has(segment) && used > liveBytes.get(segment))
      .map(([segment]) => segment)
      .sort((a, b) => liveBytes.get(a) - liveBytes.get(b) || a - b);
    // One explicit incremental pass: copy at most one journal worth of survivors and release at least two old journals
    // when a copy is needed.
    let remaining = SEGMENT - 256;
    for (const segment of candidates) {
      if (liveBytes.get(segment) <= remaining) {
        victims.add(segment);
        remaining -= liveBytes.get(segment);
      }
    }
    if (victims.size <= Number(remaining !== SEGMENT - 256)) return null;
    pending = [...entries.values()].filter((entry) => victims.has(Math.floor(entry.address / SEGMENT)));
  } else {
    for (const [strokes, text] of incoming) {
      const old = entries.get(`${identity}:${strokes.join(",")}`);
      if (old === undefined || old.operation || bytesCompare(old.text, text) !== 0) pending.push(new Edit(0, identity, 0, strokes, text));
    }
    if (!pending.length && name === null) return null;
  }

  const extents = [];
  const remap = new Map();
  snapshot.extents.forEach((extent, original) => {
    if (extent[4] === 1 || (extent[4] === 2 && !victims.has(extent[0]))) {
      remap.set(original, extents.length);
      extents.push(packExtent(extent[0], extent[1], extent[2], extent[3], extent[4], extent[5]));
    }
  });
  let highwater = snapshot.highwater;
  let epoch = snapshot.journals.reduce((best, item) => (item[5] > best ? item[5] : best), 0n);
  let offset = SEGMENT, block = null, segment = 0;
  for (const entry of pending) {
    if (offset + entry.span > SEGMENT) {
      segment = allocate();
      block = new Uint8Array(SEGMENT).fill(0xff);
      segments.set(segment, block);
      epoch += 1n;
      journalHeader(block, epoch);
      extents.push(packExtent(segment, 1, SEGMENT, 0, 2, epoch));
      offset = 256;
    }
    highwater += 1n;
    const record = encodeRecord(entry, highwater);
    block.set(record, offset);
    entries.set(entry.key, new Edit(segment * SEGMENT + offset, entry.identity, entry.operation, entry.strokes, entry.text));
    offset += entry.span;
  }
  const forward = [...entries.values()].sort((a, b) => compareKeys(outlineKey(a), outlineKey(b)));
  const reverse = [...entries.values()].filter((entry) => !entry.operation).sort((a, b) => compareKeys(textKey(a), textKey(b)));
  for (const [kind, records] of [[3, forward], [4, reverse]]) {
    if (!records.length) continue;
    const data = new Uint8Array(records.length * 4);
    records.forEach((entry, i) => view(data).setUint32(i * 4, entry.address, true));
    const count = Math.floor((data.length + SEGMENT - 1) / SEGMENT);
    const start = allocate(count);
    for (let index = 0; index < count; index++) segments.set(start + index, data.slice(index * SEGMENT, (index + 1) * SEGMENT));
    extents.push(packExtent(start, count, data.length, crc32(data), kind, kind === 3 ? OUTLINE_FIRST : 0));
  }
  const metadata = allocate();
  if (extents.length > 253) throw new NoSpace("Catalog extent limit; no data was changed");
  const rows = snapshot.rows.map((original) => {
    const row = Uint8Array.from(original);
    const base = view(row).getUint16(8, true);
    if (base !== 65535) view(row).setUint16(8, remap.get(base), true);
    return row;
  });
  if (name !== null) {
    const label = ASCII(name);
    const row = new Uint8Array(96);
    view(row).setUint32(0, identity, true);
    view(row).setUint32(4, 1, true);
    view(row).setUint16(8, 65535, true);
    row[12] = label.length;
    row.set(label, 16);
    const personalPosition = rows.findIndex((candidate) => view(candidate).getUint32(0, true) === snapshot.personal);
    rows.splice(personalPosition + 1, 0, row);
  }
  const head = new Uint8Array(32);
  view(head).setUint32(0, nextId, true);
  view(head).setUint16(4, rows.length, true);
  view(head).setUint16(6, extents.length, true);
  view(head).setBigUint64(8, highwater, true);
  const payload = new Uint8Array(32 + rows.length * 96 + extents.length * 24);
  payload.set(head, 0);
  rows.forEach((row, i) => payload.set(row, 32 + i * 96));
  extents.forEach((extent, i) => payload.set(extent, 32 + rows.length * 96 + i * 24));
  segments.set(metadata, payload);
  return {segments, metadata, payload, reclaimed: victims.size, changes: pending.length};
}
