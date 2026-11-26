// Manager milestone M3 (console-driving code): the flash map and image metadata reads, ported from
// tools/storage/native_layout.py. `console` is any object with `command(text, timeoutSeconds) -> Promise<string>`.
// golden_layout.py supplies the golden vectors; error text matches Python's where it wrote the message.
import {runtimeError} from "./errors.js";
import {SEGMENT, SMALL_POOL_END, LARGE_POOL_END, rowName} from "./snapshot.js";
import {readSnapshot, updateCommand} from "./device_reads.js";
import {inspectFree} from "./snapshot_plan.js";
import {XIP} from "./decode.js";

export const REGIONS = {
  c: "Catalog roots", x: "Reserved final segment", b: "Pinned compact image", s: "User snapshot", h: "Latest journal",
  j: "Journal", f: "Forward index", r: "Reverse index", ".": "Unreferenced", "?": "Unknown",
};

const words = (text) => { const trimmed = text.trim(); return trimmed ? trimmed.split(/\s+/) : []; };

export function parseLayout(response) {
  const parts = words(response);
  if (parts.length !== 7 || parts[0] !== "OK" || parts[1] !== "MAP1"
      || parts.slice(5).some((row) => row.length !== SMALL_POOL_END + 1)) throw new Error("Invalid flash map response");
  return parseMaps(...parts.slice(2));
}

export function parseMaps(selected, access, states, first, second) {
  if (!["0", "1"].includes(selected) || !["w", "r", "p"].includes(access) || states.length !== 2
      || [...states].some((c) => !"ve?".includes(c))) throw new Error("Invalid flash map status");
  const maps = [first, second];
  const poolEnd = first.length - 1;
  if (poolEnd !== SMALL_POOL_END && poolEnd !== LARGE_POOL_END) throw new Error("Invalid flash map size");
  for (let slot = 0; slot < 2; slot++) {
    const state = states[slot], data = maps[slot];
    if (data.length !== poolEnd + 1 || !data.startsWith("cc") || !data.endsWith("x") || [...data].some((c) => !(c in REGIONS))) {
      throw new Error("Invalid flash map geometry");
    }
    const middle = data.slice(2, poolEnd);
    if ((state === "e" && [...middle].some((c) => c !== ".")) || (state === "?" && [...middle].some((c) => c !== "?"))) {
      throw new Error("Invalid catalog region");
    }
    if (state === "v" && data.includes("?")) throw new Error("Incomplete catalog region");
  }
  const activeSlot = Number(selected);
  const active = maps[activeSlot], alternate = maps[1 - activeSlot];
  if (states[activeSlot] === "e") throw new Error("Active catalog is missing");
  const whole = !states.includes("?") && access !== "r";
  const protectedSegments = new Set();
  for (let i = 0; i < active.length; i++) if (active[i] !== "." || alternate[i] !== ".") protectedSegments.add(i);
  const [free, longest] = whole ? inspectFree(protectedSegments, poolEnd) : [null, null];
  const segments = [];
  for (let index = 0; index < active.length; index++) {
    const primary = active[index], old = alternate[index];
    let kind;
    if (index === 0 || index === 1 || index === poolEnd) kind = primary;
    else if (primary !== ".") kind = primary;
    else if ("jh".includes(old)) kind = "retained-journal";
    else if (!".?".includes(old)) kind = "retained";
    else kind = whole ? "." : "?";
    segments.push({index, kind, active: REGIONS[primary], alternate: REGIONS[old],
      shared: !".?".includes(primary) && !".?".includes(old) && index >= 2 && index < poolEnd});
  }
  return {segments, active_slot: activeSlot, states: [...states], free_segments: free, largest_free_run: longest, reserve: 3,
    staging_blocked: access !== "w"};
}

export async function readLayout(console) {
  let reply = await updateCommand(console, "layout-map");
  if (!reply.startsWith("OK MAP2 ")) return parseLayout(reply);
  const maps = ["", ""];
  let header = null;
  for (let first = 0; first <= LARGE_POOL_END; first += 256) {
    if (first) reply = await updateCommand(console, `layout-map ${first}`);
    const page = words(reply);
    if (page.length !== 11 || page[0] !== "OK" || page[1] !== "MAP2" || page[2] !== String(LARGE_POOL_END + 1)
        || page[3] !== String(first) || page.slice(9).some((data) => data.length !== 256)) throw new Error("Invalid flash map page");
    const current = page.slice(4, 9);
    if (header === null) header = current;
    else if (current.some((value, i) => value !== header[i])) {
      throw runtimeError("The catalog changed during the flash map scan. Scan again.");
    }
    for (let slot = 0; slot < 2; slot++) maps[slot] += page[9 + slot];
  }
  return parseMaps(...header.slice(2), ...maps);
}

const popcount = (value) => { let count = 0; for (let v = value >>> 0; v; v &= v - 1) count++; return count; };

// Check a single dictionary's compact tables and hints. `read(start, length)` returns a Uint8Array synchronously.
export function validateCompactLayout(read, start, size) {
  const at = (offset, length) => {
    if (offset < 0 || length < 0 || offset + length > size) throw new Error("Compact image metadata is outside its extent");
    const data = read(start + offset, length);
    return new DataView(data.buffer, data.byteOffset, data.byteLength);
  };
  const tag = (view, offset) => String.fromCharCode(...new Uint8Array(view.buffer, view.byteOffset + offset, 4));
  const base = XIP + start;
  let view = at(0, 8);
  if (tag(view, 0) !== "JSC4" || view.getUint32(4, true) !== 1) throw new Error("Unsupported compact image metadata");
  const pointer = at(36, 4).getUint32(0, true);
  view = at(size - 12, 12);
  const directory = view.getUint32(4, true);
  if (tag(view, 0) !== "SHI1" || view.getUint32(8, true) !== 1 || directory + 20 !== size) throw new Error("Unsupported compact hint directory");
  view = at(directory, 8);
  const hintStart = view.getUint32(0, true), hintCount = view.getUint32(4, true);
  const definition = at(pointer - base, 16);
  const maximum = definition.getUint8(1);
  const name = definition.getUint32(4, true), strokes = definition.getUint32(12, true);
  if (!(maximum >= 1 && maximum <= 16) || definition.getUint8(2) !== 0 || hintStart + 8 * hintCount > directory) {
    throw new Error("Unsupported compact member layout");
  }
  const nameOffset = name - base;
  const nameView = at(nameOffset, Math.min(45, size - nameOffset));
  const nameData = new Uint8Array(nameView.buffer, nameView.byteOffset, nameView.byteLength);
  const terminator = nameData.indexOf(0);
  if (terminator < 0) throw new Error("subsection not found");
  const nameLength = terminator + 1;
  // Collection pointer, definition, name, stroke table, and hint records/directory entry.
  let total = 4 + 16 + nameLength + 12 * maximum + 8 * (hintCount + 1);
  const groups = at(strokes - base, 12 * maximum);
  if (groups.byteLength % 12) throw new Error("iterative unpacking requires a buffer of a multiple of 12 bytes");
  // Like iter_unpack, use however many whole groups the reply holds.
  for (let length = 1; length <= groups.byteLength / 12; length++) {
    const mask = groups.getUint32((length - 1) * 12, true), data = groups.getUint32((length - 1) * 12 + 4, true);
    const offsets = groups.getUint32((length - 1) * 12 + 8, true);
    if (!mask) continue;
    if (!(mask >= 127 && mask <= 0x7fffff) || (mask & (mask + 1))) throw new Error("Invalid compact hash mask");
    const hashBytes = Math.floor((mask + 1) / 128) * 20;
    const end = offsets - base + hashBytes;
    const tail = at(end - 20, 20);
    const masks = [0, 1, 2, 3].map((i) => tail.getUint32(i * 4, true)), rank = tail.getUint32(16, true);
    const count = ((rank + 1) % 0x100000000) + masks.reduce((sum, bits) => sum + popcount(bits), 0);
    const dataBytes = count * (3 + 3 * length);
    if (!(count > 0 && count <= mask + 1) || !(data - base >= 0 && data - base <= offsets - base - dataBytes)) {
      throw new Error("Invalid compact data span");
    }
    total += dataBytes + hashBytes;
  }
  if (total > size) throw new Error("Compact tables exceed the image size");
}

// Use the catalog and bounded image metadata reads, without walking entries.
export async function readImages(console) {
  const answer = await updateCommand(console, "begin metadata");
  try {
    const {snapshot} = await readSnapshot(console, answer, {metadataOnly: true});
    const images = [];
    for (const row of snapshot.rows) {
      const view = new DataView(row.buffer, row.byteOffset, row.byteLength);
      const identity = view.getUint32(0, true), extent = view.getUint16(8, true), member = view.getUint8(10);
      if (extent === 65535) continue;
      if (member) throw new Error("Expected individual dictionary images");
      const [start, segments, size] = snapshot.extents[extent];
      images.push({id: identity, name: rowName(row), extent, start: Number(start), segments: Number(segments), bytes: Number(size),
        kind: row[11] ? "s" : "b"});
    }
    return images.sort((a, b) => a.start - b.start);
  } finally {
    await updateCommand(console, "abort", 5);
  }
}
