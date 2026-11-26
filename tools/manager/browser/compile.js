// Manager milestone M1/M2: the JSC4 dictionary compiler, ported from tools/dict_compiler/compile_dict.py
// (DictionaryCollectionBuilder) and compile_snapshot in tools/storage/native_snapshot.py. It must produce byte-identical
// images and pointer lists to Python (golden vectors from golden_compile.py gate this). It grew out of the measurement
// spike tools/bench/js/compile_dict.mjs, which it supersedes; the typed-array fast path (compile_fast.mjs, 4-5x quicker)
// can replace the inner loops later without changing this interface.
//
//   compile([{name, entries: [[outline, translation], ...]}], {baseAddr, maxTranslationBytes}) -> {image, pointerOffsets, warnings}
//   compileSnapshot(name, [[strokes[], textBytes], ...]) -> CompactImage {data, pointers}
import {crc32} from "./transfer_frame.js";
import {parseStroke, reader} from "./lookup.js";
import {formatStroke} from "./decode.js";
import {CompactImage} from "./snapshot_plan.js";

const encoder = new TextEncoder();
const roundUp = (v, a) => (v + a - 1) & ~(a - 1);
const popcount = (v) => { v >>>= 0; v -= (v >>> 1) & 0x55555555; v = (v & 0x33333333) + ((v >>> 2) & 0x33333333); return (((v + (v >>> 4)) & 0x0f0f0f0f) * 0x01010101) >>> 24; };

function pyRepr(text) {
  const quote = text.includes("'") && !text.includes('"') ? '"' : "'";
  let out = quote;
  for (const ch of text) {
    const code = ch.codePointAt(0);
    out += ch === quote || ch === "\\" ? "\\" + ch : ch === "\n" ? "\\n" : ch === "\r" ? "\\r" : ch === "\t" ? "\\t"
      : code < 32 || code === 127 ? "\\x" + code.toString(16).padStart(2, "0") : ch;
  }
  return out + quote;
}

// Python compares str by code point, which is the same order as UTF-8 bytes.
function compareBytes(a, b) {
  const n = Math.min(a.length, b.length);
  for (let i = 0; i < n; i++) if (a[i] !== b[i]) return a[i] - b[i];
  return a.length - b.length;
}

class Growable {
  constructor(size = 1 << 16) { this.bytes = new Uint8Array(size); this.length = 0; }
  need(extra) {
    if (this.length + extra <= this.bytes.length) return;
    const next = new Uint8Array(Math.max(this.bytes.length * 2, this.length + extra));
    next.set(this.bytes.subarray(0, this.length));
    this.bytes = next;
  }
  u8(v) { this.need(1); this.bytes[this.length++] = v; }
  u24(v) { this.need(3); this.bytes[this.length++] = v & 255; this.bytes[this.length++] = (v >>> 8) & 255; this.bytes[this.length++] = (v >>> 16) & 255; }
  u32(v) { this.need(4); this.bytes[this.length++] = v & 255; this.bytes[this.length++] = (v >>> 8) & 255; this.bytes[this.length++] = (v >>> 16) & 255; this.bytes[this.length++] = (v >>> 24) & 255; }
  append(b) { this.need(b.length); this.bytes.set(b, this.length); this.length += b.length; }
  result() { return this.bytes.slice(0, this.length); }
}

function strokeHash(strokes) {
  const data = new Uint8Array(strokes.length * 4);
  strokes.forEach((s, i) => { data[4 * i] = s & 255; data[4 * i + 1] = (s >>> 8) & 255; data[4 * i + 2] = (s >>> 16) & 255; data[4 * i + 3] = (s >>> 24) & 255; });
  return crc32(data);
}

// CompactMapBuilder: entries is a Map of stroke-list key -> {strokes, text (offset)}. Open addressing, linear probing.
function buildCompactMap(strokeLength, entries) {
  const count = entries.size;
  if (!count) return {data: new Uint8Array(0), hash: new Uint8Array(0), mask: 0};
  let blocks = Math.max(1, Math.floor((count * 2 + 127) / 128));
  let p = 1;
  while (p < blocks) p *= 2;
  blocks = p;
  const mask = blocks * 128 - 1;
  const slotStrokes = new Array(blocks * 128).fill(null);
  const slotText = new Int32Array(blocks * 128);
  for (const entry of entries.values()) {
    let h = (strokeHash(entry.strokes) & mask) >>> 0;
    while (slotStrokes[h] !== null) h = (h + 1) & mask;
    slotStrokes[h] = entry.strokes;
    slotText[h] = entry.text;
  }
  const entrySize = 3 + 3 * strokeLength;
  const data = new Growable(count * entrySize + 16), hash = new Growable(blocks * 20 + 16);
  for (let block = 0; block < blocks; block++) {
    const masks = [0, 0, 0, 0], occupied = [];
    for (let bit = 0; bit < 128; bit++) {
      const slot = block * 128 + bit;
      if (slotStrokes[slot] !== null) { masks[bit >> 5] |= 1 << (bit & 31); occupied.push(slot); }
    }
    const baseOffset = data.length / entrySize - 1; // the engine's rank is inclusive; start one below
    for (const m of masks) hash.u32(m >>> 0);
    hash.u32(baseOffset >>> 0);
    for (const slot of occupied) {
      data.u24(slotText[slot]);
      for (const s of slotStrokes[slot]) data.u24(s);
    }
  }
  return {data: data.result(), hash: hash.result(), mask};
}

export function compile(dictionaries, {baseAddr = 0, maxTranslationBytes = 127} = {}) {
  const parsed = [];
  for (const {name, entries} of dictionaries) {
    const byLength = new Map();
    let maxLen = 0;
    for (const [outline, translation] of entries) {
      let strokes;
      try {
        strokes = outline.split("/").map(parseStroke);
      } catch {
        throw new Error(`${name}: invalid outline ${pyRepr(outline)}`);
      }
      let bucket = byLength.get(strokes.length);
      if (!bucket) byLength.set(strokes.length, bucket = new Map());
      bucket.set(strokes.join(","), {strokes, text: translation}); // later duplicates replace the value and keep their place
      if (strokes.length > maxLen) maxLen = strokes.length;
    }
    parsed.push({name, byLength, maxLen});
  }

  const texts = new Set();
  for (const {byLength} of parsed) for (const bucket of byLength.values()) for (const {text} of bucket.values()) texts.add(text);
  const encodedTexts = [...texts].map((text) => [text, encoder.encode(text)]).sort((a, b) => compareBytes(a[1], b[1]));
  const textOffset = new Map(), warnings = [];
  const textBlock = new Growable(1 << 16);
  for (const [text, bytes] of encodedTexts) {
    textOffset.set(text, textBlock.length);
    if (bytes.length > maxTranslationBytes) warnings.push({bytes: bytes.length, text: Array.from(text).slice(0, 40).join("")});
    textBlock.append(bytes);
    textBlock.u8(0);
  }
  const textBytes = textBlock.result();
  for (const {byLength} of parsed) for (const bucket of byLength.values()) for (const entry of bucket.values()) entry.text = textOffset.get(entry.text);

  const strokeData = parsed.map(({name, byLength, maxLen}) => {
    const defs = [];
    for (let length = 1; length <= maxLen; length++) defs.push({length, ...buildCompactMap(length, byLength.get(length) ?? new Map())});
    return {name, defs, maxLen};
  });

  const PTR = 4, SIZED = 8, headerSize = 4 + 2 + 1 + 1 + SIZED * 3 + 4, COMPACT_DEF = 4 + PTR * 3, STROKES_DEF = 4 + PTR * 2;
  let offset = roundUp(headerSize + parsed.length * PTR, 4);
  const defOffsets = parsed.map(() => { const o = offset; offset += COMPACT_DEF; return o; });
  offset = roundUp(offset, 4);
  const nameOffsets = strokeData.map(({name}) => { const o = offset; offset += encoder.encode(name).length + 1; return o; });
  offset = roundUp(offset, 4);
  const strokesDefOffsets = strokeData.map(({defs}) => { const start = offset; const arr = defs.map(() => { const o = offset; offset += STROKES_DEF; return o; }); return [start, arr]; });
  offset = roundUp(offset, 4);
  const dataOffsets = strokeData.map(({defs}) => defs.map(({data}) => { const o = offset; offset += data.length; return o; }));
  offset = roundUp(offset, 4);
  const hashOffsets = strokeData.map(({defs}) => defs.map(({hash}) => { const o = offset; offset += hash.length; return o; }));
  offset = roundUp(offset, 4);
  const textBlockOffset = offset; offset += textBytes.length;
  const timestampEnd = offset; offset += 4;

  const buf = new Uint8Array(offset);
  const view = new DataView(buf.buffer);
  const pointerOffsets = [];
  const u32 = (o, v) => view.setUint32(o, v >>> 0, true);
  const ptr = (o, v) => { pointerOffsets.push(o); u32(o, (baseAddr + v) >>> 0); };
  const timestamp = 0x12345678;
  let at = 0;
  u32(at, 0x3443534a); at += 4; view.setUint16(at, parsed.length, true); at += 2; buf[at++] = 0; buf[at++] = 0;
  u32(at, textBytes.length); ptr(at + 4, textBlockOffset); at += SIZED;
  u32(at, 0); u32(at + 4, 0); at += SIZED;
  u32(at, 0); u32(at + 4, 0); at += SIZED;
  u32(at, timestamp); at += 4;
  for (let di = 0; di < parsed.length; di++) { ptr(at, defOffsets[di]); at += PTR; }
  strokeData.forEach(({maxLen}, di) => {
    const o = defOffsets[di];
    buf[o] = 1; buf[o + 1] = maxLen; buf[o + 2] = 0; buf[o + 3] = 0;
    ptr(o + 4, nameOffsets[di]); ptr(o + 4 + PTR, textBlockOffset); ptr(o + 4 + 2 * PTR, strokesDefOffsets[di][0]);
  });
  strokeData.forEach(({name}, di) => buf.set(encoder.encode(name), nameOffsets[di]));
  strokeData.forEach(({defs}, di) => defs.forEach(({mask}, si) => {
    const o = strokesDefOffsets[di][1][si];
    u32(o, mask); ptr(o + 4, dataOffsets[di][si]); ptr(o + 4 + PTR, hashOffsets[di][si]);
  }));
  strokeData.forEach(({defs}, di) => defs.forEach(({data}, si) => buf.set(data, dataOffsets[di][si])));
  strokeData.forEach(({defs}, di) => defs.forEach(({hash}, si) => buf.set(hash, hashOffsets[di][si])));
  buf.set(textBytes, textBlockOffset);
  u32(timestampEnd, timestamp);

  // The firmware's hint index: one best one-stroke outline per text (fewest keys, then lowest mask), sorted by text offset.
  const tail = new Growable(1 << 16);
  tail.append(buf);
  tail.append(new Uint8Array((4 - (tail.length % 4)) % 4));
  const directories = [];
  for (const {byLength} of parsed) {
    const best = new Map();
    for (const {strokes, text} of (byLength.get(1) ?? new Map()).values()) {
      const mask = strokes[0];
      if (!mask) continue;
      const old = best.get(text);
      if (old === undefined || popcount(mask) < popcount(old) || (popcount(mask) === popcount(old) && mask < old)) best.set(text, mask);
    }
    directories.push([tail.length, best.size]);
    for (const [text, mask] of [...best].sort((a, b) => a[0] - b[0])) { tail.u32(text); tail.u32(mask); }
  }
  const directoryOffset = tail.length;
  for (const [o, count] of directories) { tail.u32(o); tail.u32(count); }
  tail.u32(0x31494853); tail.u32(directoryOffset); tail.u32(parsed.length);
  return {image: tail.result(), pointerOffsets, warnings};
}

// Compile one relocatable snapshot (base address 0), then check every entry through the lookup reader.
export function compileSnapshot(name, entries) {
  if (!entries.length) throw new Error("Use a journal for an empty dictionary");
  if (entries.some(([key]) => !(key.length >= 1 && key.length <= 16) || key.some((stroke) => !(stroke > 0 && stroke < 2 ** 24)))) {
    throw new Error("This outline cannot be losslessly represented by a 24-bit JSC4 map");
  }
  if (entries.some(([, value]) => value.length > 2047 || value.includes(0))) throw new Error("Translation outside the Native limits");
  const decoder = new TextDecoder("utf-8", {fatal: true});
  const source = new Map();
  for (const [strokes, text] of entries) source.set(strokes.map(formatStroke).join("/"), decoder.decode(text));
  if (source.size !== entries.length) throw new Error("Two outlines render to the same JSC4 key");
  const {image, pointerOffsets} = compile([{name, entries: [...source]}], {baseAddr: 0, maxTranslationBytes: 2047});
  const lookup = reader(image, 0, 0, 1);
  for (const [outline, expected] of source) {
    const actual = lookup(outline);
    if (actual !== expected) throw new Error(`(${pyRepr(outline)}, ${pyRepr(expected)}, ${actual === null ? "None" : pyRepr(actual)})`);
  }
  return new CompactImage(image, pointerOffsets);
}
