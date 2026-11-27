// Manager milestone M1/M2: the steno stroke parser and the JSC4 lookup reader, ported from compile_dict.parse_stroke /
// parse_outline and jsc4.reader. Same hashing, probing and checks as the pinned engine's lookup, so a base replacement
// can compare what an outline translated to before and after. golden_replace.py supplies the golden vectors.
import {assertionError} from "./errors.js";
import {crc32} from "./transfer_frame.js";

// Order matters: the parser walks this list once, consuming matching characters (numeric aliases set the number bar too).
const STROKE_PARSER = [["#", 1], ["^", 0x800000], ["+", 0x1000000], ["1", 3], ["S", 2], ["2", 5], ["T", 4], ["K", 8],
  ["3", 17], ["P", 16], ["W", 32], ["4", 65], ["H", 64], ["R", 128], ["5", 257], ["A", 256], ["0", 513], ["O", 512],
  ["*", 1024], ["-", 0], ["E", 2048], ["U", 4096], ["6", 8193], ["F", 8192], ["R", 16384], ["7", 32769], ["P", 32768],
  ["B", 65536], ["8", 131073], ["L", 131072], ["G", 262144], ["9", 524289], ["T", 524288], ["S", 1048576],
  ["D", 2097152], ["Z", 4194304]];

export function parseStroke(text) {
  let result = 0, cursor = 0;
  for (const [character, mask] of STROKE_PARSER) {
    if (cursor < text.length && text[cursor] === character) { result |= mask; cursor++; }
  }
  if (cursor !== text.length || !result) throw new Error(`Invalid or out-of-order steno stroke: ${pyRepr(text)}`);
  return result;
}
export const parseOutline = (outline) => outline.split("/").map(parseStroke);

function pyRepr(text) { // Python's repr() for the strings steno strokes can contain
  const quote = text.includes("'") && !text.includes('"') ? '"' : "'";
  let out = quote;
  for (const ch of text) {
    const code = ch.codePointAt(0);
    out += ch === quote || ch === "\\" ? "\\" + ch : ch === "\n" ? "\\n" : ch === "\r" ? "\\r" : ch === "\t" ? "\\t"
      : code < 32 || code === 127 ? "\\x" + code.toString(16).padStart(2, "0") : ch;
  }
  return out + quote;
}

const popcount = (v) => { v >>>= 0; v -= (v >>> 1) & 0x55555555; v = (v & 0x33333333) + ((v >>> 2) & 0x33333333); return (((v + (v >>> 4)) & 0x0f0f0f0f) * 0x01010101) >>> 24; };

// jsc4.reader(blob, base, index, count): validates the image header and returns lookup(outline) -> text | null.
export function reader(blob, base = 0, index = 0, count = 1) {
  const view = new DataView(blob.buffer, blob.byteOffset, blob.byteLength);
  const word = (offset) => view.getUint32(offset, true);
  const pointer = (offset) => {
    const value = word(offset) - base;
    if (!(value >= 0 && value < blob.length)) throw assertionError(`Pointer outside image at 0x${offset.toString(16)}`);
    return value;
  };
  if (blob.length % 4 !== 0) throw assertionError("assertion failed");
  if (!(String.fromCharCode(...blob.subarray(0, 4)) === "JSC4" && word(4) === count && index < count)) throw assertionError("assertion failed");
  if (word(32) !== word(pointer(12) + word(8))) throw assertionError("Timestamp mismatch");
  const definition = pointer(36 + 4 * index);
  if (definition + 1 >= blob.length) throw new RangeError("index out of range");
  const maximum = blob[definition + 1];
  if (!(maximum > 0 && maximum <= 16)) throw assertionError("assertion failed");
  const text = pointer(definition + 8), table = pointer(definition + 12);
  const decoder = new TextDecoder("utf-8", {fatal: true});

  return (outline) => {
    const strokes = parseOutline(outline);
    const length = strokes.length;
    if (!(length > 0 && length <= maximum)) return null;
    const entry = table + 12 * (length - 1);
    const mask = word(entry);
    if (!mask) return null;
    const data = pointer(entry + 4), blocks = pointer(entry + 8);
    const keyBytes = new Uint8Array(4 * length);
    strokes.forEach((stroke, i) => new DataView(keyBytes.buffer).setUint32(4 * i, stroke, true));
    let slot = (crc32(keyBytes) & mask) >>> 0;
    for (let probe = 0; probe < mask + 1; probe++) {
      const block = blocks + 20 * Math.floor(slot / 128), bit = slot % 128;
      const part = Math.floor(bit / 32), shift = bit % 32;
      const bits = word(block + 4 * part);
      if (!(bits & (1 << shift))) return null;
      let below = 0;
      for (let j = 0; j < part; j++) below += popcount(word(block + 4 * j));
      const rank = (popcount((bits << (31 - shift)) >>> 0) + word(block + 16) + below) >>> 0;
      const offset = data + rank * (3 + 3 * length);
      if (!(data <= offset && offset + 3 + 3 * length <= blocks)) throw assertionError("assertion failed");
      let same = true;
      for (let j = 0; j < length; j++) {
        const at = offset + 3 + 3 * j;
        if (((blob[at] | (blob[at + 1] << 8) | (blob[at + 2] << 16)) >>> 0) !== strokes[j]) { same = false; break; }
      }
      if (same) {
        const start = text + ((blob[offset] | (blob[offset + 1] << 8) | (blob[offset + 2] << 16)) >>> 0);
        const end = blob.indexOf(0, start);
        if (end < 0) throw new Error("subsection not found");
        return decoder.decode(blob.subarray(start, end));
      }
      slot = (slot + 1) & mask;
      slot >>>= 0;
    }
    return null;
  };
}
