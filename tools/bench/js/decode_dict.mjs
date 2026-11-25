// JavaScript port of hybrid.compact_entries (decode every entry of a JSC4 member): what the editor and export do.
// Same enumeration order and output as Python; see compare_decode.py.
const LEFT = "STKPWHR", MIDDLE = "AO*EU", RIGHT = "FRPBLGTSDZ";

export function formatStroke(mask) {
  if (!mask || mask >>> 25) throw new Error("Unsupported compact stroke");
  let left = "", middle = "", right = "";
  for (let i = 0; i < 7; i++) if (mask & (1 << (1 + i))) left += LEFT[i];
  for (let i = 0; i < 5; i++) if (mask & (1 << (8 + i))) middle += MIDDLE[i];
  for (let i = 0; i < 10; i++) if (mask & (1 << (13 + i))) right += RIGHT[i];
  return (mask & 1 ? "#" : "") + (mask & (1 << 23) ? "^" : "") + (mask & (1 << 24) ? "+" : "") + left + middle + (right && !middle ? "-" : "") + right;
}

const popcount = (v) => { v -= (v >>> 1) & 0x55555555; v = (v & 0x33333333) + ((v >>> 2) & 0x33333333); return (((v + (v >>> 4)) & 0x0f0f0f0f) * 0x01010101) >>> 24; };

export function compactEntries(blob, base = 0, member = 0) {
  const view = new DataView(blob.buffer, blob.byteOffset, blob.byteLength);
  const word = (o) => view.getUint32(o, true);
  const pointer = (o) => { const v = word(o) - base; if (v < 0 || v >= blob.length) throw new Error("Compact pointer bounds"); return v; };
  const decoder = new TextDecoder();
  const strokeNames = new Map();
  const name = (mask) => { let s = strokeNames.get(mask); if (s === undefined) strokeNames.set(mask, s = formatStroke(mask)); return s; };
  const definition = pointer(36 + 4 * member);
  const text = pointer(definition + 8), table = pointer(definition + 12), maximum = blob[definition + 1];
  const out = [];
  for (let length = 1; length <= maximum; length++) {
    const entry = table + 12 * (length - 1), mask = word(entry);
    if (!mask) continue;
    const data = pointer(entry + 4), blocks = pointer(entry + 8), blockCount = (mask + 1) / 128;
    let count = 0;
    for (let b = 0; b < blockCount; b++) for (let i = 0; i < 4; i++) count += popcount(word(blocks + 20 * b + 4 * i));
    const stride = 3 + 3 * length;
    for (let index = 0; index < count; index++) {
      const position = data + index * stride;
      const start = text + (blob[position] | (blob[position + 1] << 8) | (blob[position + 2] << 16));
      let end = start;
      while (blob[end] !== 0) end++;
      let outline = "";
      for (let i = 0; i < length; i++) {
        const o = position + 3 + 3 * i;
        outline += (i ? "/" : "") + name(blob[o] | (blob[o + 1] << 8) | (blob[o + 2] << 16));
      }
      out.push([outline, decoder.decode(blob.subarray(start, end))]);
    }
  }
  return out;
}
