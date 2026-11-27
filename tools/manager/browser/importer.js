// Manager milestone M1: Plover JSON import and export, ported from parse_named and write_json in
// tools/dictionaries/import_personal.py. JSON.parse cannot be used: it hides duplicate keys and reorders integer-like keys,
// and the compiler's placement (and so the image bytes) depends on file order. This is an ordered reader that accepts and
// rejects what Python's json.loads(object_pairs_hook=unique_pairs) does, and refuses a duplicate key at every level.
// golden_import.py supplies the golden vectors; error text matches where the Python code wrote the message.
import {parseStroke} from "./lookup.js";

const encoder = new TextEncoder();
const WS = new Set([" ", "\t", "\n", "\r"]);

export function pyRepr(text) {
  const quote = text.includes("'") && !text.includes('"') ? '"' : "'";
  let out = quote;
  for (const ch of text) {
    const code = ch.codePointAt(0);
    out += ch === quote || ch === "\\" ? "\\" + ch : ch === "\n" ? "\\n" : ch === "\r" ? "\\r" : ch === "\t" ? "\\t"
      : code < 32 || code === 127 ? "\\x" + code.toString(16).padStart(2, "0") : ch;
  }
  return out + quote;
}

class JsonError extends Error { constructor(message, pos = 0) { super(message); this.name = "JSONDecodeError"; this.pos = pos; } }
const OBJECT = Symbol("object");

// Returns {[OBJECT]: true, pairs: [[key, value], ...]} for objects, arrays for arrays, and plain values otherwise.
export function parseOrderedJson(source) {
  if (source.startsWith("﻿")) throw new JsonError("Unexpected UTF-8 BOM (decode using utf-8-sig)", 0);
  let i = 0;
  const fail = (what, pos = i) => { throw new JsonError(`${what}: char ${pos}`, pos); };
  const skip = () => { while (i < source.length && WS.has(source[i])) i++; };

  function string() {
    const begin = i; // Python reports an unterminated string at its opening quote
    i++; // opening quote
    let out = "";
    for (;;) {
      if (i >= source.length) fail("Unterminated string", begin);
      const ch = source[i];
      if (ch === '"') { i++; return out; }
      if (ch === "\\") {
        const next = source[i + 1];
        if (next === undefined) fail("Unterminated string", begin);
        const simple = {'"': '"', "\\": "\\", "/": "/", b: "\b", f: "\f", n: "\n", r: "\r", t: "\t"}[next];
        if (simple !== undefined) { out += simple; i += 2; continue; }
        if (next !== "u") fail("Invalid \\escape");
        const hex = source.slice(i + 2, i + 6);
        if (!/^[0-9a-fA-F]{4}$/.test(hex)) fail("Invalid \\uXXXX escape", i + 1);
        let code = parseInt(hex, 16);
        i += 6;
        if (code >= 0xd800 && code <= 0xdbff && source[i] === "\\" && source[i + 1] === "u") {
          const low = source.slice(i + 2, i + 6);
          if (/^[0-9a-fA-F]{4}$/.test(low)) {
            const lowCode = parseInt(low, 16);
            if (lowCode >= 0xdc00 && lowCode <= 0xdfff) { code = 0x10000 + ((code - 0xd800) << 10) + (lowCode - 0xdc00); i += 6; }
          }
        }
        out += String.fromCodePoint(code);
        continue;
      }
      if (ch.charCodeAt(0) < 0x20) fail("Invalid control character");
      out += ch;
      i++;
    }
  }

  function value() {
    const ch = source[i];
    if (ch === '"') return string();
    if (ch === "{") return object();
    if (ch === "[") return array();
    if (source.startsWith("true", i)) { i += 4; return true; }
    if (source.startsWith("false", i)) { i += 5; return false; }
    if (source.startsWith("null", i)) { i += 4; return null; }
    if (source.startsWith("NaN", i)) { i += 3; return NaN; }
    if (source.startsWith("Infinity", i)) { i += 8; return Infinity; }
    if (source.startsWith("-Infinity", i)) { i += 9; return -Infinity; }
    const number = /-?(?:0|[1-9]\d*)(\.\d+)?([eE][-+]?\d+)?/y;
    number.lastIndex = i;
    const match = number.exec(source);
    if (!match) fail("Expecting value");
    i += match[0].length;
    return Number(match[0]);
  }

  function object() {
    i++;
    const pairs = [];
    // Python's object_pairs_hook runs when the object closes, so a syntax error later in the same object is reported first.
    const close = () => {
      const seen = new Set();
      for (const [key] of pairs) {
        if (seen.has(key)) throw new Error(`Duplicate JSON key ${pyRepr(key)}`);
        seen.add(key);
      }
      return {[OBJECT]: true, pairs};
    };
    skip();
    if (source[i] === "}") { i++; return close(); }
    for (;;) {
      skip();
      if (source[i] !== '"') fail("Expecting property name enclosed in double quotes");
      const key = string();
      skip();
      if (source[i] !== ":") fail("Expecting ':' delimiter");
      i++;
      skip();
      const item = value();
      pairs.push([key, item]);
      skip();
      if (source[i] === "}") { i++; return close(); }
      if (source[i] !== ",") fail("Expecting ',' delimiter");
      i++;
    }
  }

  function array() {
    i++;
    const items = [];
    skip();
    if (source[i] === "]") { i++; return items; }
    for (;;) {
      skip();
      items.push(value());
      skip();
      if (source[i] === "]") { i++; return items; }
      if (source[i] !== ",") fail("Expecting ',' delimiter");
      i++;
    }
  }

  skip();
  if (i >= source.length) fail("Expecting value");
  const result = value();
  skip();
  if (i !== source.length) fail("Extra data");
  return result;
}

const hasLoneSurrogate = (text) => /[\ud800-\udbff](?![\udc00-\udfff])|(?<![\ud800-\udbff])[\udc00-\udfff]/.test(text);
const equalBytes = (a, b) => a.length === b.length && a.every((value, i) => value === b[i]);

// The entries of a Plover JSON object and the outline text of each: {entries: [[strokes[], textBytes]], names: [[strokes[], outline]]},
// both in first-appearance order.
export function parseNamed(source) {
  const data = parseOrderedJson(source);
  if (!(data && typeof data === "object" && OBJECT in data)) throw new Error("The file must be a Plover JSON object mapping outlines to translations.");
  const entries = new Map(), names = new Map();
  for (const [outline, translation] of data.pairs) {
    const strokes = outline.split("/").map(parseStroke);
    if (strokes.length > 16) throw new Error(`${pyRepr(outline)} has more than 16 strokes.`);
    if (typeof translation !== "string" || translation.includes("\0")) throw new Error(`${pyRepr(outline)} needs a text translation without NUL bytes.`);
    if (hasLoneSurrogate(translation)) throw Object.assign(new Error("surrogates not allowed"), {name: "UnicodeEncodeError"});
    const text = encoder.encode(translation);
    if (text.length > 2047) throw new Error(`${pyRepr(outline)} has a translation longer than 2,047 UTF-8 bytes.`);
    const key = strokes.join(",");
    const old = entries.get(key);
    if (old && !equalBytes(old.text, text)) throw new Error(`${pyRepr(outline)} uses the same keys as another outline with different text.`);
    if (old) old.text = text; else entries.set(key, {strokes, text});
    if (!names.has(key)) names.set(key, {strokes, outline});
  }
  return {entries: [...entries.values()].map((e) => [e.strokes, e.text]), names: [...names.values()].map((n) => [n.strokes, n.outline])};
}

// Python's json.dump(ensure_ascii=False, indent=0) plus a trailing newline, for the export file.
function jsonString(text) {
  let out = '"';
  for (const ch of text) {
    const code = ch.codePointAt(0);
    out += ch === '"' ? '\\"' : ch === "\\" ? "\\\\" : ch === "\n" ? "\\n" : ch === "\r" ? "\\r" : ch === "\t" ? "\\t"
      : ch === "\b" ? "\\b" : ch === "\f" ? "\\f" : code < 32 ? "\\u" + code.toString(16).padStart(4, "0") : ch;
  }
  return out + '"';
}
export function toPloverJson(entries, names) {
  const nameOf = new Map(names.map(([strokes, outline]) => [strokes.join(","), outline]));
  const decoder = new TextDecoder("utf-8", {fatal: true});
  const lines = entries.map(([strokes, text]) => `${jsonString(nameOf.get(strokes.join(",")))}: ${jsonString(decoder.decode(text))}`);
  return (lines.length ? "{\n" + lines.join(",\n") + "\n}" : "{}") + "\n";
}
