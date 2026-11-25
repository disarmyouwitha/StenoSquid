// JavaScript port of tools/dict_compiler/compile_dict.py (JSC4 compact-map compiler).
// A measurement spike: it must produce BYTE-IDENTICAL images to the Python compiler (see
// compare_compile.py / compare_compile.mjs), and is timed against it. No dependencies.
//
// Order matters: the Python compiler places entries by linear probing in dict insertion order, and
// JavaScript objects reorder integer-like keys, so the JSON is read by a small ordered parser that
// never builds an object.

const STROKE_PARSER = [["#", 1], ["^", 0x800000], ["+", 0x1000000],
  ["1", 3], ["S", 2], ["2", 5], ["T", 4], ["K", 8], ["3", 17], ["P", 16], ["W", 32], ["4", 65], ["H", 64], ["R", 128],
  ["5", 257], ["A", 256], ["0", 513], ["O", 512], ["*", 1024], ["-", 0], ["E", 2048], ["U", 4096], ["6", 8193],
  ["F", 8192], ["R", 16384], ["7", 32769], ["P", 32768], ["B", 65536], ["8", 131073], ["L", 131072], ["G", 262144],
  ["9", 524289], ["T", 524288], ["S", 1048576], ["D", 2097152], ["Z", 4194304]];
const PARSER_CODES = Uint8Array.from(STROKE_PARSER.map(([c]) => c.charCodeAt(0)));
const PARSER_MASKS = Int32Array.from(STROKE_PARSER.map(([, m]) => m));

const CRC_TABLE = (() => {
  const table = new Uint32Array(256);
  for (let n = 0; n < 256; n++) {
    let c = n;
    for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
    table[n] = c >>> 0;
  }
  return table;
})();

export function crc32(bytes, start = 0, end = bytes.length) {
  let crc = 0xffffffff;
  for (let i = start; i < end; i++) crc = CRC_TABLE[(crc ^ bytes[i]) & 0xff] ^ (crc >>> 8);
  return (crc ^ 0xffffffff) >>> 0;
}

// Parse a steno stroke from s[start, end) (char codes); returns the mask or throws.
function parseStroke(codes, start, end) {
  let result = 0;
  let cursor = start;
  for (let i = 0; i < PARSER_CODES.length; i++) {
    if (cursor < end && codes[cursor] === PARSER_CODES[i]) { result |= PARSER_MASKS[i]; cursor++; }
  }
  if (cursor !== end || !result) throw new Error("Invalid or out-of-order steno stroke");
  return result;
}

// Ordered reader for a flat JSON object {string: string}. Returns [keys, values] in file order.
export function readFlatJson(bytes) {
  const decoder = new TextDecoder();
  const keys = [];
  const values = [];
  let i = 0;
  const n = bytes.length;
  const ws = () => { while (i < n && (bytes[i] === 32 || bytes[i] === 10 || bytes[i] === 13 || bytes[i] === 9)) i++; };
  const string = () => {
    if (bytes[i] !== 34) throw new Error("Expected a JSON string at " + i);
    const begin = ++i;
    let escaped = false;
    while (bytes[i] !== 34) { if (bytes[i] === 92) { escaped = true; i++; } i++; }
    const slice = bytes.subarray(begin, i++);
    const text = decoder.decode(slice);
    return escaped ? JSON.parse('"' + text.replace(/\n/g, "\\n") + '"') : text;
  };
  ws();
  if (bytes[i++] !== 123) throw new Error("Expected a JSON object");
  ws();
  if (bytes[i] === 125) return [keys, values];
  for (;;) {
    ws(); keys.push(string()); ws();
    if (bytes[i++] !== 58) throw new Error("Expected ':'");
    ws(); values.push(string()); ws();
    const c = bytes[i++];
    if (c === 125) return [keys, values];
    if (c !== 44) throw new Error("Expected ',' or '}'");
  }
}

const popcount = (v) => { v -= (v >>> 1) & 0x55555555; v = (v & 0x33333333) + ((v >>> 2) & 0x33333333); return (((v + (v >>> 4)) & 0x0f0f0f0f) * 0x01010101) >>> 24; };
const roundUp = (v, a) => (v + a - 1) & ~(a - 1);
const encoder = new TextEncoder();

// Compare two byte strings the way Python compares str by code point (UTF-8 order is identical).
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
  for (let i = 0; i < strokes.length; i++) {
    const s = strokes[i];
    data[4 * i] = s & 255; data[4 * i + 1] = (s >>> 8) & 255; data[4 * i + 2] = (s >>> 16) & 255; data[4 * i + 3] = (s >>> 24) & 255;
  }
  return crc32(data);
}

function buildCompactMap(strokeLength, entries) {
  const count = entries.size;
  if (!count) return { data: new Uint8Array(0), hash: new Uint8Array(0), mask: 0 };
  let blocks = Math.max(1, Math.floor((count * 2 + 127) / 128));
  let p = 1;
  while (p < blocks) p *= 2;
  blocks = p;
  const slotsTotal = blocks * 128;
  const mask = slotsTotal - 1;
  const slotStrokes = new Array(slotsTotal).fill(null);
  const slotText = new Int32Array(slotsTotal);
  for (const [, entry] of entries) {
    let h = (strokeHash(entry.strokes) & mask) >>> 0;
    while (slotStrokes[h] !== null) h = (h + 1) & mask;
    slotStrokes[h] = entry.strokes;
    slotText[h] = entry.text;
  }
  const entrySize = 3 + 3 * strokeLength;
  const data = new Growable(count * entrySize + 16);
  const hash = new Growable(blocks * 20 + 16);
  for (let block = 0; block < blocks; block++) {
    const masks = [0, 0, 0, 0];
    const occupied = [];
    for (let bit = 0; bit < 128; bit++) {
      const slot = block * 128 + bit;
      if (slotStrokes[slot] !== null) { masks[bit >> 5] |= 1 << (bit & 31); occupied.push(slot); }
    }
    const baseOffset = data.length / entrySize - 1;
    for (const m of masks) hash.u32(m >>> 0);
    hash.u32(baseOffset >>> 0);
    for (const slot of occupied) {
      data.u24(slotText[slot]);
      for (const s of slotStrokes[slot]) data.u24(s);
    }
  }
  return { data: data.result(), hash: hash.result(), mask };
}

// dictionaries: [{name, keys: string[], values: string[]}]. Returns Uint8Array (the JSC4 image).
export function compile(dictionaries, baseAddr = 0) {
  const parsed = [];
  for (const { name, keys, values } of dictionaries) {
    const byLength = new Map();
    let maxLen = 0;
    const encodedKeys = keys.map((k) => encoder.encode(k));
    for (let e = 0; e < keys.length; e++) {
      const codes = encodedKeys[e];
      const strokes = [];
      let begin = 0;
      for (let i = 0; i <= codes.length; i++) {
        if (i === codes.length || codes[i] === 47) { strokes.push(parseStroke(codes, begin, i)); begin = i + 1; }
      }
      const len = strokes.length;
      let bucket = byLength.get(len);
      if (!bucket) byLength.set(len, bucket = new Map());
      bucket.set(strokes.join(","), { strokes, text: values[e] }); // Map keeps first-insertion order, like dict
      if (len > maxLen) maxLen = len;
    }
    parsed.push({ name, byLength, maxLen });
  }

  const texts = new Set();
  for (const { byLength } of parsed) for (const bucket of byLength.values()) for (const { text } of bucket.values()) texts.add(text);
  const encodedTexts = [...texts].map((t) => [t, encoder.encode(t)]);
  encodedTexts.sort((a, b) => compareBytes(a[1], b[1]));
  const textOffset = new Map();
  const textBlock = new Growable(1 << 20);
  for (const [text, bytes] of encodedTexts) { textOffset.set(text, textBlock.length); textBlock.append(bytes); textBlock.u8(0); }
  const textBytes = textBlock.result();
  for (const { byLength } of parsed) for (const bucket of byLength.values()) for (const entry of bucket.values()) entry.text = textOffset.get(entry.text);

  const strokeData = parsed.map(({ name, byLength, maxLen }) => {
    const defs = [];
    for (let length = 1; length <= maxLen; length++) defs.push({ length, ...buildCompactMap(length, byLength.get(length) ?? new Map()) });
    return { name, defs, maxLen };
  });

  const PTR = 4, SIZED = 8, headerSize = 4 + 2 + 1 + 1 + SIZED * 3 + 4, dictPtrs = parsed.length * PTR;
  const COMPACT_DEF = 4 + PTR * 3, STROKES_DEF = 4 + PTR * 2;
  let offset = roundUp(headerSize + dictPtrs, 4);
  const defOffsets = parsed.map(() => { const o = offset; offset += COMPACT_DEF; return o; });
  offset = roundUp(offset, 4);
  const nameOffsets = strokeData.map(({ name }) => { const o = offset; offset += encoder.encode(name).length + 1; return o; });
  offset = roundUp(offset, 4);
  const strokesDefOffsets = strokeData.map(({ defs }) => { const start = offset; const arr = defs.map(() => { const o = offset; offset += STROKES_DEF; return o; }); return [start, arr]; });
  offset = roundUp(offset, 4);
  const dataOffsets = strokeData.map(({ defs }) => defs.map(({ data }) => { const o = offset; offset += data.length; return o; }));
  offset = roundUp(offset, 4);
  const hashOffsets = strokeData.map(({ defs }) => defs.map(({ hash }) => { const o = offset; offset += hash.length; return o; }));
  offset = roundUp(offset, 4);
  const textBlockOffset = offset; offset += textBytes.length;
  const timestampEnd = offset; offset += 4;

  const buf = new Uint8Array(offset);
  const view = new DataView(buf.buffer);
  const pointerOffsets = [];
  const u32 = (o, v) => view.setUint32(o, v >>> 0, true);
  const ptr = (o, v) => { pointerOffsets.push(o); u32(o, (baseAddr + v) >>> 0); };
  const timestamp = 0x12345678;
  let p0 = 0;
  u32(p0, 0x3443534a); p0 += 4; view.setUint16(p0, parsed.length, true); p0 += 2; buf[p0++] = 0; buf[p0++] = 0;
  u32(p0, textBytes.length); ptr(p0 + 4, textBlockOffset); p0 += SIZED;
  u32(p0, 0); u32(p0 + 4, 0); p0 += SIZED;
  u32(p0, 0); u32(p0 + 4, 0); p0 += SIZED;
  u32(p0, timestamp); p0 += 4;
  for (let di = 0; di < parsed.length; di++) { ptr(p0, defOffsets[di]); p0 += PTR; }
  strokeData.forEach(({ maxLen }, di) => {
    const o = defOffsets[di];
    buf[o] = 1; buf[o + 1] = maxLen; buf[o + 2] = 0; buf[o + 3] = 0;
    ptr(o + 4, nameOffsets[di]); ptr(o + 4 + PTR, textBlockOffset); ptr(o + 4 + 2 * PTR, strokesDefOffsets[di][0]);
  });
  strokeData.forEach(({ name }, di) => { buf.set(encoder.encode(name), nameOffsets[di]); });
  strokeData.forEach(({ defs }, di) => defs.forEach(({ mask }, si) => {
    const o = strokesDefOffsets[di][1][si];
    u32(o, mask); ptr(o + 4, dataOffsets[di][si]); ptr(o + 4 + PTR, hashOffsets[di][si]);
  }));
  strokeData.forEach(({ defs }, di) => defs.forEach(({ data }, si) => buf.set(data, dataOffsets[di][si])));
  strokeData.forEach(({ defs }, di) => defs.forEach(({ hash }, si) => buf.set(hash, hashOffsets[di][si])));
  buf.set(textBytes, textBlockOffset);
  u32(timestampEnd, timestamp);

  // One best one-stroke outline per text (the firmware's hint index).
  const tail = new Growable(1 << 16);
  tail.append(buf);
  tail.append(new Uint8Array((4 - (tail.length % 4)) % 4));
  const directories = [];
  for (const { byLength } of parsed) {
    const best = new Map();
    for (const { strokes, text } of (byLength.get(1) ?? new Map()).values()) {
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
  return tail.result();
}
