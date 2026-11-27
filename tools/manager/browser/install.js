// Installing the factory dictionaries onto a keyboard, ported from tools/storage/build_native_image.py (build, for the
// installer: no personal entries and no prebuilt collection) and tools/storage/native_install.py (check_image, provision,
// write_pool, prepare_install_image). The image is built in memory from the pinned sources bundled in the page, checked, and
// written to the keyboard's shared pool over the console.
import {runtimeError} from "./errors.js";
import {compile} from "./compile.js";
import {parseNamed} from "./importer.js";
import {crc32} from "./transfer_frame.js";
import {Snapshot} from "./snapshot.js";
import {sha256Hex} from "./snapshot_plan.js";
import {kibibytes} from "./editor.js";
import {FACTORY_CAPABILITIES} from "./state.js";

const SEGMENT = 65536;
const SMALL_POOL_END = 255, LARGE_POOL_END = 1023;
const POOL_ENDS = [SMALL_POOL_END, LARGE_POOL_END];
const XIP = 0x12000000;
const COMMIT = 0x434f4d54;
// (identity, name, enabled, source path under build/)
const SOURCES = [
  [1, "user_dictionary", true, null],
  [2, "Lapwing Commands", true, "extras/lapwing-commands.json"],
  [3, "Lapwing Numbers", true, "extras/lapwing-numbers.json"],
  [4, "Lapwing UK Additions", true, "extras/lapwing-uk-additions.json"],
  [5, "Lapwing Proper Nouns", true, "extras/lapwing-proper-nouns.json"],
  [6, "Lapwing Base", true, "lapwing/lapwing-base.json"],
  [7, "Plover Base", false, "plover/plover-main.json"],
];
const FACTORY_KEYS = {"lapwing-commands": 2, "lapwing-numbers": 3, "lapwing-uk": 4, "lapwing-proper": 5, "lapwing-base": 6, plover: 7};
export const UNPROVISIONED = "NATIVE2 UNPROVISIONED 0";

const encoder = new TextEncoder();
const view = (bytes) => new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
const concat = (...parts) => {
  const out = new Uint8Array(parts.reduce((sum, part) => sum + part.length, 0));
  let at = 0;
  for (const part of parts) { out.set(part, at); at += part.length; }
  return out;
};
const hexBytes = (hex) => Uint8Array.from(hex.match(/../g), (pair) => parseInt(pair, 16));
const equalBytes = (a, b) => a.length === b.length && a.every((byte, i) => byte === b[i]);
const isErased = (bytes) => bytes.every((byte) => byte === 0xff);
const grouped = (number) => number.toLocaleString("en-US");

export function packExtent(segment, span, bytes, crc, kind, auxiliary) {
  const out = new Uint8Array(24);
  const v = view(out);
  v.setUint16(0, segment, true); v.setUint16(2, span, true); v.setUint32(4, bytes, true);
  v.setUint32(8, crc, true); v.setUint32(12, kind, true); v.setBigUint64(16, BigInt(auxiliary), true);
  return out;
}

function catalogHeader(generation, payload) {
  const header = new Uint8Array(24);
  header.set(encoder.encode("SQBCAT02"));
  view(header).setBigUint64(8, BigInt(generation), true);
  view(header).setUint32(16, payload.length, true);
  view(header).setUint32(20, crc32(payload), true);
  const tail = new Uint8Array(8);
  view(tail).setUint32(0, crc32(header), true);
  view(tail).setUint32(4, COMMIT, true);
  return concat(header, tail);
}

export function writeJournalHeader(image, segment, epoch) {
  const header = new Uint8Array(32);
  header.set(encoder.encode("SQJSEG01"));
  view(header).setBigUint64(8, BigInt(epoch), true);
  view(header).setUint32(16, crc32(header.subarray(0, 16)), true);
  image.set(header, segment * SEGMENT);
  return packExtent(segment, 1, SEGMENT, 0, 2, epoch);
}

// The (outline, translation) pairs of a pinned source file, in file order.
function sourcePairs(raw) {
  const {entries, names} = parseNamed(new TextDecoder("utf-8", {fatal: true}).decode(raw));
  const decoder = new TextDecoder("utf-8", {fatal: true});
  return entries.map(([, text], i) => [names[i][1], decoder.decode(text)]);
}

// build_native_image.build(source_root, packages=...). `readSource(path)` resolves to the bytes of a pinned source file.
export async function buildImage(readSource, packages = null, poolEnd = SMALL_POOL_END) {
  if (!POOL_ENDS.includes(poolEnd)) throw new Error("Unsupported flash geometry");
  const chosen = new Set(packages ?? Object.keys(FACTORY_KEYS));
  if (!chosen.size || [...chosen].some((key) => !Object.hasOwn(FACTORY_KEYS, key))) throw new Error("Select at least one factory dictionary.");
  const installed = new Set([...chosen].map((key) => FACTORY_KEYS[key]));
  const image = new Uint8Array(poolEnd * SEGMENT).fill(0xff);
  const extents = [], dictionaries = [], manifest = [];
  let nextSegment = 2;
  const location = new Map();
  for (const [identity, name, , source] of SOURCES.slice(1)) {
    const raw = await readSource(source);
    const digest = hexBytes(await sha256Hex(raw));
    if (!installed.has(identity)) { location.set(name, [65535, 0, digest]); continue; }
    location.set(name, [extents.length, 0, digest]);
    const address = nextSegment * SEGMENT;
    const {image: binary} = compile([{name, entries: sourcePairs(raw)}], {baseAddr: XIP + address});
    const count = Math.floor((binary.length + SEGMENT - 1) / SEGMENT);
    if (nextSegment + count > poolEnd - 3) throw new Error("Base images exhaust the three-segment reserve");
    image.set(binary, address);
    const crc = crc32(binary);
    extents.push(packExtent(nextSegment, count, binary.length, crc, 1, 0));
    manifest.push({name, address, bytes: binary.length, segments: count, crc32: crc,
      dictionaries: [{id: identity, index: 0, name, source_sha256: Array.from(digest, (b) => b.toString(16).padStart(2, "0")).join("")}]});
    nextSegment += count;
  }
  const rows = SOURCES.map(([identity, name, enabled]) => [identity, name, enabled]);
  if (chosen.has("plover") && !chosen.has("lapwing-base")) rows[rows.length - 1] = [7, "Plover Base", true];
  const nextId = 8;
  const personalId = rows.find(([, name]) => name === "user_dictionary")[0];
  for (let [identity, name, enabled] of rows) {
    const [base, index, digest] = location.get(name) ?? [65535, 0, new Uint8Array(32)];
    const encoded = encoder.encode(name);
    const row = new Uint8Array(96);
    if (base === 65535 && location.has(name)) { enabled = false; row[11] = 2; }
    const v = view(row);
    v.setUint32(0, identity, true); v.setUint32(4, enabled ? 1 : 0, true); v.setUint16(8, base, true); row[10] = index;
    row[12] = encoded.length;
    row.set(encoded, 16);
    row.set(digest, 64);
    dictionaries.push(row);
  }
  const journalSegment = nextSegment;
  if (journalSegment >= poolEnd - 3) throw new Error("The initial journal must leave the three-segment reserve");
  extents.push(writeJournalHeader(image, journalSegment, 1));
  nextSegment = journalSegment + 1;
  if (nextSegment > poolEnd - 3 || extents.length > 253) throw new Error("The initial journal exceeds catalog capacity or the maintenance reserve");
  const prefix = new Uint8Array(32);
  view(prefix).setUint32(0, nextId, true); view(prefix).setUint16(4, dictionaries.length, true);
  view(prefix).setUint16(6, extents.length, true); view(prefix).setBigUint64(8, 0n, true);
  const payload = concat(prefix, ...dictionaries, ...extents);
  for (let slot = 0; slot < 2; slot++) image.set(concat(catalogHeader(slot + 1, payload), payload), slot * SEGMENT);
  const report = {schema: 2, format: "SQBCAT02", bytes: image.length, crc32: crc32(image), sha256: await sha256Hex(image), catalog_bytes: payload.length,
    base_images: manifest, free_segments: poolEnd - nextSegment, provision: "explicit clean erase; no migration", personal_id: personalId, personal_entries: 0};
  return [image, report];
}

// native_install.check_image: the image must be a sound shared-pool image that matches its manifest. Resolves to the set of
// factory identities the image leaves uninstalled.
export async function checkImage(image, manifest) {
  const poolEnd = Math.floor(image.length / SEGMENT);
  if (!POOL_ENDS.includes(poolEnd) || image.length % SEGMENT || manifest.format !== "SQBCAT02") throw new Error("Not a native shared-pool image");
  if (crc32(image) !== manifest.crc32 || await sha256Hex(image) !== manifest.sha256) throw new Error("Native image checksum differs from its manifest");
  let absent = null;
  const factoryNames = new Map(SOURCES.filter(([, , , source]) => source).map(([identity, name]) => [identity, encoder.encode(name)]));
  const v = view(image);
  for (const root of [0, SEGMENT]) {
    const payloadBytes = v.getUint32(root + 16, true), crc = v.getUint32(root + 20, true), headerCrc = v.getUint32(root + 24, true), committed = v.getUint32(root + 28, true);
    const payload = image.subarray(root + 32, root + 32 + payloadBytes);
    const rows = v.getUint16(root + 36, true), extents = v.getUint16(root + 38, true);
    const extentOffset = 32 + rows * 96;
    if (String.fromCharCode(...image.subarray(root, root + 8)) !== "SQBCAT02" || rows > 64 || extents > 253 || payloadBytes !== extentOffset + extents * 24
        || committed !== COMMIT || crc32(image.subarray(root, root + 24)) !== headerCrc || crc32(payload) !== crc) throw new Error("Invalid Native image catalog");
    const omitted = new Set(), factoryExtents = new Set();
    const p = view(payload);
    for (let index = 0; index < rows; index++) {
      const row = payload.subarray(32 + index * 96, 32 + (index + 1) * 96);
      const identity = p.getUint32(32 + index * 96, true), enabled = p.getUint32(32 + index * 96 + 4, true), base = p.getUint16(32 + index * 96 + 8, true);
      const member = row[10], marker = row[11], nameBytes = row[12];
      if (!marker) {
        if (factoryNames.has(identity)) {
          if (member || base >= extents || factoryExtents.has(base)) throw new Error("Factory dictionaries must have individual flash images.");
          const at = extentOffset + base * 24;
          const start = p.getUint16(at, true), span = p.getUint16(at + 2, true), size = p.getUint32(at + 4, true), kind = p.getUint32(at + 12, true);
          if (!(start >= 2 && start < poolEnd) || !(span >= 1 && span <= poolEnd - start) || !(size >= 8 && size <= span * SEGMENT) || kind !== 1
              || !equalBytes(image.subarray(start * SEGMENT, start * SEGMENT + 8), Uint8Array.of(0x4a, 0x53, 0x43, 0x34, 1, 0, 0, 0))) throw new Error("Invalid individual factory image");
          factoryExtents.add(base);
        }
        continue;
      }
      if (marker === 1) throw new Error("Experimental snapshot images cannot be provisioned over USB.");
      if (marker !== 2 || enabled || base !== 65535 || member || !row.subarray(64, 96).some(Boolean)
          || !factoryNames.has(identity) || !equalBytes(factoryNames.get(identity), row.subarray(16, 16 + nameBytes)) || omitted.has(identity)) {
        throw new Error("Invalid uninstalled factory dictionary");
      }
      omitted.add(identity);
    }
    if (absent !== null && (absent.size !== omitted.size || [...absent].some((identity) => !omitted.has(identity)))) throw new Error("Native catalogs disagree about factory dictionaries");
    absent = omitted;
    await Snapshot.load((at, size) => image.subarray(at, at + size), root + 32, payloadBytes, crc, {strict: true, poolEnd});
  }
  return absent;
}

export async function command(console, text, timeout = 30) {
  const result = await console.command("native_install " + text, timeout);
  if (!result.startsWith("OK")) throw runtimeError(result);
  return result;
}

export const beginForPool = (text, poolEnd) => (poolEnd === LARGE_POOL_END ? `large ${poolEnd} ${text}` : text);

export async function readPoolEnd(console) {
  const reply = await console.command("native_install geometry", 30);
  if (reply === "ERR No native installation active" || reply === "ERR Command unavailable on this keyboard") return SMALL_POOL_END;
  if (reply === `OK POOL1 ${SMALL_POOL_END}`) return SMALL_POOL_END;
  if (reply === `OK POOL1 ${LARGE_POOL_END}`) return LARGE_POOL_END;
  throw runtimeError(`Cannot determine the external flash geometry. ${reply}`);
}

function toBase64(bytes) {
  let text = "";
  for (let i = 0; i < bytes.length; i++) text += String.fromCharCode(bytes[i]);
  return btoa(text);
}

export async function writePool(console, image, progress, {finish = true, writeLabel = "Writing native image"} = {}) {
  // Write all referenced data before making either catalog visible at boot.
  const catalogEnd = 2 * SEGMENT, blockSize = 512;
  const offsets = [];
  for (let at = catalogEnd; at < image.length; at += blockSize) if (!isErased(image.subarray(at, at + blockSize))) offsets.push(at);
  for (let at = 0; at < catalogEnd; at += blockSize) if (!isErased(image.subarray(at, at + blockSize))) offsets.push(at);
  const total = offsets.length * blockSize;
  const poolEnd = Math.floor(image.length / SEGMENT);
  for (let segment = 0; segment < poolEnd; segment++) {
    await command(console, `erase ${segment}`);
    if (segment % 16 === 0 || segment + 1 === poolEnd) progress(`Erasing shared pool: ${segment + 1}/${poolEnd}`);
  }
  if (total) progress(`${writeLabel}: 0 / ${kibibytes(total)} KiB (0%)`);
  let count = 0;
  for (const at of offsets) {
    count++;
    await command(console, `write ${at} ${toBase64(image.subarray(at, at + blockSize))}`);
    const sent = count * blockSize;
    if (sent === total || sent % (128 * 1024) === 0) progress(`${writeLabel}: ${kibibytes(sent)} / ${kibibytes(total)} KiB (${Math.floor(sent * 100 / total)}%)`);
  }
  if (finish) {
    progress("Verifying the complete shared pool; the left will reboot afterward");
    await command(console, "finish", 180);
  }
}

export async function provision(console, image, manifest, progress = () => {}, {expected_status: expectedStatus = null} = {}) {
  await checkImage(image, manifest);
  if (await console.command(FACTORY_CAPABILITIES, 30) !== "OK FACTORY2") throw runtimeError("Update the left firmware to install individual factory dictionaries.");
  const status = await console.command("native_install status", 30);
  if (!status.startsWith("NATIVE2 ")) throw runtimeError("Flash the matching Native firmware before installing: " + status);
  if (expectedStatus !== null && status !== expectedStatus) throw runtimeError("The keyboard’s dictionary status changed. Reconnect before setting up.");
  const poolEnd = await readPoolEnd(console);
  const expectedLength = poolEnd * SEGMENT;
  if (image.length > expectedLength) throw new Error("The Native image is larger than this keyboard's flash.");
  if (image.length < expectedLength) image = concat(image, new Uint8Array(expectedLength - image.length).fill(0xff));
  const begin = `begin CLEAR ${crc32(image)}`;
  await command(console, poolEnd === LARGE_POOL_END ? `large ${poolEnd} ${begin}` : begin);
  try {
    await writePool(console, image, progress, {writeLabel: "Writing base dictionaries"});
  } catch (error) {
    try { await command(console, "abort", 2); } catch { /* the original failure is the one to report */ }
    throw error;
  }
}

// prepare_install_image(packages=...): [image, manifest], built from the sources bundled in the page (`archive()` is the zip).
export function makeImagePreparer(readSource) {
  return (packages) => buildImage(readSource, packages);
}
