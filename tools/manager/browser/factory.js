// The pinned factory dictionaries bundled in the offline page, ported from manager_editor.factory_source. The Python Manager read
// build/<directory>/<file>.json from its runtime; the page carries the same files inside the runtime zip (base64 in the HTML), so
// this reads that zip directly: no Python needed. A file is used only when its SHA-256 equals the digest the keyboard reports for
// the installed factory image; otherwise null, and the editor reads the dictionary from the keyboard instead.
import {parseNamed} from "./importer.js";
import {formatStroke} from "./decode.js";
import {sha256Hex} from "./snapshot_plan.js";
import {compile} from "./compile.js";
import {reader} from "./lookup.js";
import {assertionError} from "./errors.js";

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

// manifest.py: the SHA-256 of each pinned source file, by dictionary name.
const PINNED = {
  "Lapwing Commands": "6160e19084c77381f66aeda72473d807bcf3ea7f0c6303a37189ca60bf59ff3d",
  "Lapwing Numbers": "d104b9d6f8ea7935bdf3129d69bcdc4100e2b55fa717b41be2307043a4bd4b73",
  "Lapwing UK Additions": "e6d3190d0417c9912b34ebf9a3d02be08935d5c1c1b46bb47f0b580ed21717d3",
  "Lapwing Proper Nouns": "aef0205fa4bc6bcb1bc11c199b0c01737ada5ffa7a0f0a56d48154d024ce66e8",
  "Lapwing Base": "2750c20466bc5040d69167e02095776531afb6c3c1f6f662bba01006e7e0e484",
  "Plover Base": "6570873c7f703ecd39c9870083f643db3475b49462a8db80fe22fe33ac3cfe92",
};

// native_base_update.prepare: compile the pinned factory dictionary (from the bundled archive) into the relocatable image that
// restoring it installs, and check every entry through the lookup reader. Resolves to {contents, pointers, digest}.
export function makePrepare(archive) {
  return async (name) => {
    const source = [...SOURCES.values()].find(([label]) => label === name);
    const digest = PINNED[name];
    if (!source || !digest) throw new Error(`Unknown pinned dictionary: ${name}`);
    const zip = archive();
    const raw = zip ? await readZipEntry(zip, `build/${source[1]}`) : null;
    if (raw === null) throw new Error(`The bundled factory dictionary is missing: ${name}.`);
    if (await sha256Hex(raw) !== digest) throw new Error(`Pinned dictionary checksum mismatch: ${name}. Run tools/dictionaries/prepare.py first.`);
    const {entries, names} = parseNamed(new TextDecoder("utf-8", {fatal: true}).decode(raw));
    const decoder = new TextDecoder("utf-8", {fatal: true});
    const pairs = entries.map(([, text], i) => [names[i][1], decoder.decode(text)]);
    const {image, pointerOffsets} = compile([{name, entries: pairs}], {baseAddr: 0});
    const lookup = reader(image, 0, 0, 1);
    for (const [outline, expected] of pairs) {
      const actual = lookup(outline);
      if (actual !== expected) throw assertionError(`${outline}: expected ${expected}, found ${actual}`);
    }
    return {contents: image, pointers: pointerOffsets, digest: Uint8Array.from(digest.match(/../g), (pair) => parseInt(pair, 16))};
  };
}
