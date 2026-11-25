// Typed-array JSC4 compiler: same output as tools/dict_compiler/compile_dict.py (byte for byte), written the way
// a production implementation would be: no per-entry objects, no string decoding of keys or values, text
// deduplication by sorting byte slices, hash-table dedupe of outlines. Compared against Python in
// compare_compile.py. Inputs are raw JSON bytes of flat {outline: translation} objects.

export { crc32 } from "./compile_dict.mjs";
import { crc32 } from "./compile_dict.mjs";

const STROKE_CODES = [35, 94, 43, 49, 83, 50, 84, 75, 51, 80, 87, 52, 72, 82, 53, 65, 48, 79, 42, 45, 69, 85, 54, 70, 82, 55, 80, 66, 56, 76, 71, 57, 84, 83, 68, 90];
const STROKE_MASKS = [1, 0x800000, 0x1000000, 3, 2, 5, 4, 8, 17, 16, 32, 65, 64, 128, 257, 256, 513, 512, 1024, 0, 2048, 4096, 8193, 8192, 16384, 32769, 32768, 65536, 131073, 131072, 262144, 524289, 524288, 1048576, 2097152, 4194304];
const NCODES = STROKE_CODES.length;
const CODES = Uint8Array.from(STROKE_CODES);
const MASKS = Int32Array.from(STROKE_MASKS);
const CRC_TABLE = (() => { const t = new Uint32Array(256); for (let n = 0; n < 256; n++) { let c = n; for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1; t[n] = c >>> 0; } return t; })();
const encoder = new TextEncoder();
const popcount = (v) => { v -= (v >>> 1) & 0x55555555; v = (v & 0x33333333) + ((v >>> 2) & 0x33333333); return (((v + (v >>> 4)) & 0x0f0f0f0f) * 0x01010101) >>> 24; };
const roundUp = (v, a) => (v + a - 1) & ~(a - 1);

class Bytes {
  constructor(size = 1 << 16) { this.a = new Uint8Array(size); this.n = 0; }
  need(k) { if (this.n + k > this.a.length) { const b = new Uint8Array(Math.max(this.a.length * 2, this.n + k)); b.set(this.a.subarray(0, this.n)); this.a = b; } }
  push(v) { this.need(1); this.a[this.n++] = v; }
  extend(src, s, e) { this.need(e - s); this.a.set(src.subarray(s, e), this.n); this.n += e - s; }
}
class Ints {
  constructor(size = 1 << 16) { this.a = new Int32Array(size); this.n = 0; }
  push(v) { if (this.n === this.a.length) { const b = new Int32Array(this.a.length * 2); b.set(this.a); this.a = b; } this.a[this.n++] = v; }
}

function utf8(code, out) {
  if (code < 0x80) out.push(code);
  else if (code < 0x800) { out.push(0xc0 | (code >> 6)); out.push(0x80 | (code & 63)); }
  else if (code < 0x10000) { out.push(0xe0 | (code >> 12)); out.push(0x80 | ((code >> 6) & 63)); out.push(0x80 | (code & 63)); }
  else { out.push(0xf0 | (code >> 18)); out.push(0x80 | ((code >> 12) & 63)); out.push(0x80 | ((code >> 6) & 63)); out.push(0x80 | (code & 63)); }
}

// Scan a flat JSON object. Strings are copied (unescaped) into `arena`; returns Int32Array [ks, ke, vs, ve]*count.
function scan(json, arena) {
  const spans = new Ints(1 << 18);
  let i = 0;
  const n = json.length;
  const ws = () => { while (i < n && (json[i] === 32 || json[i] === 10 || json[i] === 13 || json[i] === 9)) i++; };
  const string = () => {
    if (json[i] !== 34) throw new Error("Expected a JSON string at " + i);
    i++;
    const start = arena.n;
    let run = i;
    for (;;) {
      const c = json[i];
      if (c === 34) { arena.extend(json, run, i); i++; break; }
      if (c === 92) {
        arena.extend(json, run, i);
        const e = json[i + 1];
        i += 2;
        if (e === 117) {
          let code = parseInt(String.fromCharCode(json[i], json[i + 1], json[i + 2], json[i + 3]), 16); i += 4;
          if (code >= 0xd800 && code < 0xdc00 && json[i] === 92 && json[i + 1] === 117) {
            const low = parseInt(String.fromCharCode(json[i + 2], json[i + 3], json[i + 4], json[i + 5]), 16);
            code = 0x10000 + ((code - 0xd800) << 10) + (low - 0xdc00); i += 6;
          }
          utf8(code, arena);
        } else arena.push(e === 110 ? 10 : e === 116 ? 9 : e === 114 ? 13 : e === 98 ? 8 : e === 102 ? 12 : e);
        run = i;
      } else i++;
    }
    return [start, arena.n];
  };
  ws();
  if (json[i++] !== 123) throw new Error("Expected a JSON object");
  ws();
  if (json[i] === 125) return spans;
  for (;;) {
    ws(); const k = string(); ws();
    if (json[i++] !== 58) throw new Error("Expected ':'");
    ws(); const v = string(); ws();
    spans.push(k[0]); spans.push(k[1]); spans.push(v[0]); spans.push(v[1]);
    const c = json[i++];
    if (c === 125) return spans;
    if (c !== 44) throw new Error("Expected ',' or '}'");
  }
}

// dictionaries: [{name: string, json: Uint8Array}]. Returns the JSC4 image as a Uint8Array.
export function compileFast(dictionaries, baseAddr = 0) {
  const arena = new Bytes(1 << 22);
  const dicts = dictionaries.map(({ name, json }) => {
    const spans = scan(json, arena);
    const count = spans.n / 4;
    const strokeFlat = new Ints(count * 3 + 16); // stroke masks of every entry, back to back
    const entryStart = new Int32Array(count + 1);
    const entryLen = new Uint8Array(count);
    let maxLen = 0;
    const a = arena.a;
    for (let e = 0; e < count; e++) {
      entryStart[e] = strokeFlat.n;
      let begin = spans.a[4 * e], len = 0;
      const end = spans.a[4 * e + 1];
      for (let p = begin; p <= end; p++) {
        if (p === end || a[p] === 47) {
          let result = 0, cursor = begin;
          for (let t = 0; t < NCODES; t++) if (cursor < p && a[cursor] === CODES[t]) { result |= MASKS[t]; cursor++; }
          if (cursor !== p || !result) throw new Error("Invalid or out-of-order steno stroke");
          strokeFlat.push(result); len++; begin = p + 1;
        }
      }
      entryLen[e] = len;
      if (len > maxLen) maxLen = len;
    }
    entryStart[count] = strokeFlat.n;
    // Group by length, deduplicating outlines that parse to the same strokes (first position, last value).
    const groups = Array.from({ length: maxLen + 1 }, () => new Ints(1024)); // entry ids, insertion order
    const valueOf = new Int32Array(count); // entry id -> span index of its (last) value
    const seen = Array.from({ length: maxLen + 1 }, () => new Map());
    const sf = strokeFlat.a;
    for (let e = 0; e < count; e++) {
      const len = entryLen[e], at = entryStart[e];
      const key = len === 1 ? sf[at] : len === 2 ? sf[at] * 33554432 + sf[at + 1] : Array.prototype.join.call(sf.subarray(at, at + len), ",");
      const map = seen[len];
      const first = map.get(key);
      if (first === undefined) { map.set(key, e); groups[len].push(e); valueOf[e] = e; } else valueOf[first] = e;
    }
    return { name, spans, strokeFlat: sf, entryStart, groups, valueOf, maxLen };
  });

  // Shared text block: sorted unique values of the surviving entries, by UTF-8 bytes (= code point order).
  const a = arena.a;
  const valueRefs = []; // [dictIndex, spanIndex]
  dicts.forEach((d, di) => d.groups.forEach((g) => { for (let k = 0; k < g.n; k++) valueRefs.push(di * 0x4000000 + d.valueOf[g.a[k]]); }));
  const refs = Int32Array.from(valueRefs);
  const spanStart = (r) => dicts[(r / 0x4000000) | 0].spans.a[4 * (r % 0x4000000) + 2];
  const spanEnd = (r) => dicts[(r / 0x4000000) | 0].spans.a[4 * (r % 0x4000000) + 3];
  const order = Int32Array.from(refs.keys());
  const cmp = (x, y) => {
    const rx = refs[x], ry = refs[y];
    const sx = spanStart(rx), ex = spanEnd(rx), sy = spanStart(ry), ey = spanEnd(ry);
    const n = Math.min(ex - sx, ey - sy);
    for (let i = 0; i < n; i++) { const d = a[sx + i] - a[sy + i]; if (d) return d; }
    return (ex - sx) - (ey - sy);
  };
  order.sort(cmp);
  const textOffsetOfRef = new Int32Array(refs.length);
  const textBlock = new Bytes(1 << 20);
  let previous = -1;
  let currentOffset = 0;
  for (let k = 0; k < order.length; k++) {
    const idx = order[k];
    if (previous < 0 || cmp(previous, idx) !== 0) {
      currentOffset = textBlock.n;
      const r = refs[idx];
      textBlock.extend(a, spanStart(r), spanEnd(r));
      textBlock.push(0);
      previous = idx;
    }
    textOffsetOfRef[idx] = currentOffset;
  }
  // Compact maps per (dictionary, length).
  let ref = 0;
  const built = dicts.map((d) => {
    const defs = [];
    for (let length = 1; length <= d.maxLen; length++) {
      const g = d.groups[length];
      const count = g.n;
      if (!count) { defs.push({ data: new Uint8Array(0), hash: new Uint8Array(0), mask: 0 }); continue; }
      let blocks = Math.max(1, Math.floor((count * 2 + 127) / 128)), p = 1;
      while (p < blocks) p *= 2;
      blocks = p;
      const total = blocks * 128, mask = total - 1;
      const slot = new Int32Array(total).fill(-1);
      const texts = new Int32Array(count);
      for (let k = 0; k < count; k++) {
        const e = g.a[k], at = d.entryStart[e];
        texts[k] = textOffsetOfRef[ref++];
        let crc = 0xffffffff;
        for (let s = 0; s < length; s++) {
          const v = d.strokeFlat[at + s];
          crc = CRC_TABLE[(crc ^ (v & 255)) & 255] ^ (crc >>> 8);
          crc = CRC_TABLE[(crc ^ ((v >>> 8) & 255)) & 255] ^ (crc >>> 8);
          crc = CRC_TABLE[(crc ^ ((v >>> 16) & 255)) & 255] ^ (crc >>> 8);
          crc = CRC_TABLE[(crc ^ ((v >>> 24) & 255)) & 255] ^ (crc >>> 8);
        }
        let h = ((crc ^ 0xffffffff) & mask) >>> 0;
        while (slot[h] !== -1) h = (h + 1) & mask;
        slot[h] = k;
      }
      const entrySize = 3 + 3 * length;
      const data = new Uint8Array(count * entrySize), hash = new Uint8Array(blocks * 20);
      const dv = new DataView(hash.buffer);
      let placed = 0;
      for (let b = 0; b < blocks; b++) {
        const masks = [0, 0, 0, 0];
        const baseOffset = placed - 1;
        for (let bit = 0; bit < 128; bit++) {
          const k = slot[b * 128 + bit];
          if (k < 0) continue;
          masks[bit >> 5] |= 1 << (bit & 31);
          const o = placed++ * entrySize, e = g.a[k], at = d.entryStart[e], t = texts[k];
          data[o] = t & 255; data[o + 1] = (t >>> 8) & 255; data[o + 2] = (t >>> 16) & 255;
          for (let s = 0; s < length; s++) { const v = d.strokeFlat[at + s]; data[o + 3 + 3 * s] = v & 255; data[o + 4 + 3 * s] = (v >>> 8) & 255; data[o + 5 + 3 * s] = (v >>> 16) & 255; }
        }
        for (let m = 0; m < 4; m++) dv.setUint32(b * 20 + 4 * m, masks[m] >>> 0, true);
        dv.setUint32(b * 20 + 16, baseOffset >>> 0, true);
      }
      defs.push({ data, hash, mask });
    }
    return { name: d.name, defs, maxLen: d.maxLen, groups: d.groups, d };
  });

  // Layout and serialisation: identical to compile_dict.py.
  const textBytes = textBlock.a.subarray(0, textBlock.n);
  const PTR = 4, SIZED = 8, headerSize = 4 + 2 + 1 + 1 + SIZED * 3 + 4, COMPACT_DEF = 4 + PTR * 3, STROKES_DEF = 4 + PTR * 2;
  let offset = roundUp(headerSize + built.length * PTR, 4);
  const defOffsets = built.map(() => { const o = offset; offset += COMPACT_DEF; return o; });
  offset = roundUp(offset, 4);
  const nameBytes = built.map(({ name }) => encoder.encode(name));
  const nameOffsets = nameBytes.map((nb) => { const o = offset; offset += nb.length + 1; return o; });
  offset = roundUp(offset, 4);
  const strokesDef = built.map(({ defs }) => { const start = offset; const arr = defs.map(() => { const o = offset; offset += STROKES_DEF; return o; }); return [start, arr]; });
  offset = roundUp(offset, 4);
  const dataOffsets = built.map(({ defs }) => defs.map(({ data }) => { const o = offset; offset += data.length; return o; }));
  offset = roundUp(offset, 4);
  const hashOffsets = built.map(({ defs }) => defs.map(({ hash }) => { const o = offset; offset += hash.length; return o; }));
  offset = roundUp(offset, 4);
  const textOffset = offset; offset += textBytes.length;
  const timestampEnd = offset; offset += 4;
  const base = new Uint8Array(offset), view = new DataView(base.buffer);
  const u32 = (o, v) => view.setUint32(o, v >>> 0, true);
  const ptr = (o, v) => u32(o, (baseAddr + v) >>> 0);
  let q = 0;
  u32(q, 0x3443534a); q += 4; view.setUint16(q, built.length, true); q += 2; q += 2;
  u32(q, textBytes.length); ptr(q + 4, textOffset); q += SIZED + 2 * SIZED;
  u32(q, 0x12345678); q += 4;
  built.forEach((_, di) => { ptr(q, defOffsets[di]); q += PTR; });
  built.forEach(({ maxLen }, di) => { const o = defOffsets[di]; base[o] = 1; base[o + 1] = maxLen; ptr(o + 4, nameOffsets[di]); ptr(o + 8, textOffset); ptr(o + 12, strokesDef[di][0]); });
  nameBytes.forEach((nb, di) => base.set(nb, nameOffsets[di]));
  built.forEach(({ defs }, di) => defs.forEach(({ mask }, si) => { const o = strokesDef[di][1][si]; u32(o, mask); ptr(o + 4, dataOffsets[di][si]); ptr(o + 8, hashOffsets[di][si]); }));
  built.forEach(({ defs }, di) => defs.forEach(({ data }, si) => base.set(data, dataOffsets[di][si])));
  built.forEach(({ defs }, di) => defs.forEach(({ hash }, si) => base.set(hash, hashOffsets[di][si])));
  base.set(textBytes, textOffset);
  u32(timestampEnd, 0x12345678);

  // Hint index: best one-stroke outline per text.
  const pad = (4 - (base.length % 4)) % 4;
  const tail = [];
  const directories = [];
  let length = base.length + pad;
  // text offsets are stored per (dict, length) in encounter order; recompute for length 1 explicitly
  ref = 0;
  const hintBases = [];
  built.forEach(({ d }) => {
    const per = [];
    for (let l = 1; l < d.groups.length; l++) { per[l] = ref; ref += d.groups[l].n; }
    hintBases.push(per);
  });
  built.forEach(({ d }, di) => {
    const best = new Map();
    const g = d.groups[1];
    if (g) for (let k = 0; k < g.n; k++) {
      const mask = d.strokeFlat[d.entryStart[g.a[k]]];
      if (!mask) continue;
      const text = textOffsetOfRef[hintBases[di][1] + k];
      const old = best.get(text);
      if (old === undefined || popcount(mask) < popcount(old) || (popcount(mask) === popcount(old) && mask < old)) best.set(text, mask);
    }
    directories.push([length, best.size]);
    for (const [text, mask] of [...best].sort((x, y) => x[0] - y[0])) { tail.push(text, mask); length += 8; }
  });
  const directoryOffset = length;
  const out = new Uint8Array(length + directories.length * 8 + 12);
  out.set(base);
  const ov = new DataView(out.buffer);
  let w = base.length + pad;
  for (let k = 0; k < tail.length; k++, w += 4) ov.setUint32(w, tail[k] >>> 0, true);
  for (const [o, count] of directories) { ov.setUint32(w, o, true); ov.setUint32(w + 4, count, true); w += 8; }
  ov.setUint32(w, 0x31494853, true); ov.setUint32(w + 4, directoryOffset, true); ov.setUint32(w + 8, directories.length, true);
  return out;
}
