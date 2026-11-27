// The pinned factory dictionaries bundled in the offline page, ported from manager_editor.factory_source. The Python Manager read
// build/<directory>/<file>.json from its runtime; the page carries the same files inside the runtime zip (base64 in the HTML), so
// this reads that zip directly: no Python needed. A file is used only when its SHA-256 equals the digest the keyboard reports for
// the installed factory image; otherwise null, and the editor reads the dictionary from the keyboard instead.
import {parseNamed} from "./importer.js";
import {formatStroke} from "./decode.js";
import {sha256Hex} from "./snapshot_plan.js";

// (identity, name) -> path of the source file under build/ (build_native_image.SOURCES).
const SOURCES = new Map([
  [2, ["Lapwing Commands", "extras/lapwing-commands.json"]], [3, ["Lapwing Numbers", "extras/lapwing-numbers.json"]],
  [4, ["Lapwing UK Additions", "extras/lapwing-uk-additions.json"]], [5, ["Lapwing Proper Nouns", "extras/lapwing-proper-nouns.json"]],
  [6, ["Lapwing Base", "lapwing/lapwing-base.json"]], [7, ["Plover Base", "plover/plover-main.json"]],
]);

// One file out of a zip held in memory (stored or deflated). Returns the bytes, or null when the name is not in the archive.
export async function readZipEntry(zip, wanted) {
  const view = new DataView(zip.buffer, zip.byteOffset, zip.byteLength);
  let end = zip.length - 22;
  while (end >= 0 && view.getUint32(end, true) !== 0x06054b50) end--;
  if (end < 0) throw new Error("The bundled archive is damaged.");
  const entries = view.getUint16(end + 10, true);
  let at = view.getUint32(end + 16, true);
  const decoder = new TextDecoder();
  for (let i = 0; i < entries; i++) {
    if (view.getUint32(at, true) !== 0x02014b50) throw new Error("The bundled archive is damaged.");
    const method = view.getUint16(at + 10, true), compressed = view.getUint32(at + 20, true), size = view.getUint32(at + 24, true);
    const nameLength = view.getUint16(at + 28, true), extra = view.getUint16(at + 30, true), comment = view.getUint16(at + 32, true);
    const local = view.getUint32(at + 42, true);
    const name = decoder.decode(zip.subarray(at + 46, at + 46 + nameLength));
    at += 46 + nameLength + extra + comment;
    if (name !== wanted) continue;
    const start = local + 30 + view.getUint16(local + 26, true) + view.getUint16(local + 28, true);
    const data = zip.subarray(start, start + compressed);
    if (method === 0) return data.slice();
    if (method !== 8) throw new Error("The bundled archive uses an unsupported compression method.");
    const stream = new Blob([data]).stream().pipeThrough(new DecompressionStream("deflate-raw"));
    const bytes = new Uint8Array(await new Response(stream).arrayBuffer());
    if (bytes.length !== size) throw new Error("The bundled archive is damaged.");
    return bytes;
  }
  return null;
}

// `archive()` returns the zip bytes (Uint8Array) or null when there is no bundle. The result is the editor's factorySource:
// (identity, name, sha256) -> Map(outline -> text) | null, remembering the last six like Python's lru_cache.
export function makeFactorySource(archive) {
  const remembered = new Map();
  return async (identity, name, digest) => {
    const source = SOURCES.get(identity);
    if (!source || source[0] !== name || !digest) return null;
    const key = `${identity}\n${name}\n${digest}`;
    if (remembered.has(key)) {
      const value = remembered.get(key);
      remembered.delete(key);
      remembered.set(key, value);
      return value === null ? null : new Map(value);
    }
    let result = null;
    const zip = archive();
    const raw = zip ? await readZipEntry(zip, `build/${source[1]}`) : null;
    if (raw !== null && await sha256Hex(raw) === digest) {
      const {entries} = parseNamed(new TextDecoder("utf-8", {fatal: true}).decode(raw));
      const decoder = new TextDecoder("utf-8", {fatal: true});
      result = new Map(entries.map(([strokes, text]) => [strokes.map(formatStroke).join("/"), decoder.decode(text)]));
    }
    remembered.set(key, result);
    if (remembered.size > 6) remembered.delete(remembered.keys().next().value);
    return result === null ? null : new Map(result);
  };
}
