// The Manager's USB repack, offline merge and restore-original jobs, ported from tools/manager/manager_repack.py. The Python version
// kept verified backups in ~/.stenosquid/repack/<sha256(serial)>/<token>/; the page keeps them in the recovery folder the user
// chose, through `store`:
//   store.save(identity, token, name, text)  -> Promise; creates the folder, refuses to replace an existing file
//   store.load(identity, token, name)        -> Promise<string>; rejects when the file is missing
//   store.latest(identity)                   -> Promise<token | null>; the newest directory holding both original.json and attempt.json
// `identity` is the SHA-256 of the USB serial and `token` is 32 hex digits. Nothing is erased until the original backup and the
// optimized bundle are both saved, read back unchanged and verified.
import {runtimeError} from "./errors.js";
import {backupAll} from "./backup.js";
import {kibibytes} from "./editor.js";
import {readImages, readLayout} from "./layout.js";
import {reconstruct} from "./recovery.js";
import {recoverUsb, restoreRepack} from "./recover.js";
import {RESERVE, buildMergeRepack, buildRepack, overlayRecordSpan, segmentCount} from "./repack.js";
import {offlineMergeToken, pyJson, sha256Hex} from "./snapshot_plan.js";
import {SEGMENT} from "./snapshot.js";
import {need, overlayUsage, parseJson} from "./state.js";

const SMALL_POOL_END = 255;
const encoder = new TextEncoder();
const isString = (value) => typeof value === "string";
const isInt = (value) => typeof value === "number" && Number.isInteger(value);
const sum = (values) => values.reduce((total, value) => total + value, 0);
const get = (object, key, fallback = null) => (Object.hasOwn(object, key) ? object[key] : fallback);

// json.dumps(value, sort_keys=True) with Python's default separators and ASCII escaping; integer-keyed dicts sort numerically.
function pyDumps(value) {
  if (value === null || value === undefined) return "null";
  if (typeof value === "boolean") return value ? "true" : "false";
  if (typeof value === "number" || typeof value === "bigint") return String(value);
  if (typeof value === "string") return pyJson(value);
  if (Array.isArray(value)) return "[" + value.map(pyDumps).join(", ") + "]";
  const keys = Object.keys(value);
  const numeric = keys.length > 0 && keys.every((key) => /^(0|[1-9][0-9]*)$/.test(key));
  keys.sort(numeric ? (a, b) => Number(a) - Number(b) : (a, b) => (a < b ? -1 : a > b ? 1 : 0));
  return "{" + keys.map((key) => `${pyJson(key)}: ${pyDumps(value[key])}`).join(", ") + "}";
}

// Place the existing compact images and a conservative journal estimate.
export function projectLayout(images, usage, poolEnd = SMALL_POOL_END) {
  const kinds = ["c", "c", ...Array(poolEnd - 2).fill("."), "x"];
  let nextBlock = 2;
  const projected = [];
  const place = (kind, blocks) => {
    const start = nextBlock;
    nextBlock += blocks;
    if (nextBlock > poolEnd - RESERVE) throw new Error("The projected repack could use the maintenance reserve.");
    for (let i = start; i < nextBlock; i++) kinds[i] = kind;
    return start;
  };
  let journalPlaced = false;
  const order = [...images].sort((a, b) => ((a.kind === "b" ? a.id : 8) - (b.kind === "b" ? b.id : 8)) || (a.extent - b.extent));
  for (const image of order) {
    if (image.kind === "s" && !journalPlaced) { place("j", 1); journalPlaced = true; }
    projected.push({...image, start: place(image.kind, image.segments)});
  }
  if (!journalPlaced) place("j", 1);
  const rows = Object.values(usage);
  const liveBytes = sum(rows.map((row) => row.bytes));
  // The actual builder packs unsplit records in outline order. Each closed block holds at least one block's payload minus the largest
  // possible gap. Count the upper bound so the preview never promises more free space.
  const payload = SEGMENT - 256;
  const largestRecord = overlayRecordSpan(Array(16).fill(1), new Uint8Array(2047));
  const guaranteed = payload - largestRecord + 1;
  const extra = Math.max(0, liveBytes - payload);
  place("j", Math.floor((extra + guaranteed - 1) / guaranteed));
  place("f", segmentCount(4 * sum(rows.map((row) => row.count))));
  place("r", segmentCount(4 * sum(rows.map((row) => row.entries))));
  return {segments: kinds.map((kind, index) => ({index, kind})), images: projected, projected: true, free_segments: poolEnd - nextBlock,
    largest_free_run: poolEnd - nextBlock};
}

export function makeRepackJobs({opener, clock, store = null, now = undefined, token = () => {
  const bytes = globalThis.crypto.getRandomValues(new Uint8Array(16));
  return Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
}}) {
  const deps = {opener, clock};
  const folder = () => {
    if (!store) throw runtimeError("Choose a recovery folder before repacking or restoring dictionaries.");
    return store;
  };
  const backupIdentity = async (serial) => {
    if (!serial) throw new Error("Connect a keyboard with an identifiable USB serial.");
    return sha256Hex(encoder.encode(serial));
  };
  const planToken = (value) => {
    if (!isString(value) || !/^[0-9a-f]{32}$/.test(value)) throw new Error("Preview a repack for this keyboard first.");
    return value;
  };

  async function previewRepack(console, progress) {
    if (await console.command("native_install update repack-capabilities", 30) !== "OK REPACK1"
        || await console.command("native_install update recovery-capabilities", 30) !== "OK RECOVERY1") {
      throw runtimeError("This keyboard does not offer guarded USB repacking.");
    }
    progress("Reading the flash layout and journal metadata…");
    const start = parseJson(await console.command("user_dictionary_status", 30));
    const state = {generation: need(start, "generation"), tail: need(start, "tail")};
    const before = await readLayout(console);
    if (before.staging_blocked || before.free_segments === null) throw runtimeError("The flash layout is busy or unavailable. Scan and preview again.");
    const images = await readImages(console);
    const usage = await overlayUsage(console, {metadataOnly: true});
    const end = parseJson(await console.command("user_dictionary_status", 30));
    if (need(end, "generation") !== state.generation || need(end, "tail") !== state.tail
        || await console.command("native_install status", 30) !== `NATIVE2 READY ${state.generation}`) {
      throw runtimeError("Flash changed during the preview. Scan and preview again.");
    }
    const after = projectLayout(images, usage, ((before.segments && before.segments.length) || SMALL_POOL_END + 1) - 1);
    const result = {serial: console.serial, state, before: {largest_free_run: before.largest_free_run, free_segments: before.free_segments}, after};
    if (after.largest_free_run > before.largest_free_run || (after.largest_free_run === before.largest_free_run && after.free_segments > before.free_segments)) {
      result.token = (await sha256Hex(encoder.encode(pyDumps([result, before.segments, usage])))).slice(0, 32);
    }
    return result;
  }

  function backupForRewrite(console, progress) {
    const label = "Backing up dictionaries and saved edits";
    progress(`${label}…`);
    const report = (received, total) => progress(`${label}: ${kibibytes(received)} / ${kibibytes(total)} KiB (${Math.floor(received * 100 / total)}%)`);
    return backupAll(console, now ? {onProgress: report, now} : {onProgress: report});
  }

  async function captureApprovedRepack(console, request, progress) {
    const serial = console.serial;
    if (get(request, "serial") !== serial || get(request, "confirm") !== serial) throw new Error("Confirm the USB serial before rewriting the dictionary flash.");
    const plan = await previewRepack(console, progress);
    if (!plan.token || get(request, "token") !== plan.token) throw new Error("Flash changed since the approved preview. Preview USB repack again.");
    const source = await backupForRewrite(console, progress);
    const status = parseJson(await console.command("user_dictionary_status", 30));
    if (source.capture.generation !== plan.state.generation || Object.entries(plan.state).some(([key, value]) => need(status, key) !== value)) {
      throw runtimeError("Flash changed during backup. No erase was started. Preview USB repack again.");
    }
    return [source, plan];
  }

  // Save a file, read it back, and refuse to go on unless the folder holds exactly what was written.
  async function saveDurably(identity, directory, name, document) {
    const text = JSON.stringify(document) + "\n";
    await folder().save(identity, directory, name, text);
    if (await folder().load(identity, directory, name) !== text) throw runtimeError("The persisted recovery file differs. No flash erase was started.");
    return text;
  }
  async function readBundle(identity, directory, name, expected = null) {
    const text = await folder().load(identity, directory, name);
    if (expected !== null && text !== expected) throw runtimeError("The persisted recovery file differs. No flash erase was started.");
    return [JSON.parse(text), await sha256Hex(encoder.encode(text))];
  }

  async function installVerifiedBundle(source, target, serial, progress) {
    const directory = planToken(token());
    const identity = await backupIdentity(serial);
    const originalText = await saveDurably(identity, directory, "original", source);
    const optimizedText = await saveDurably(identity, directory, "optimized", target);
    const [, targetHash] = await readBundle(identity, directory, "optimized", optimizedText);
    await saveDurably(identity, directory, "attempt", {original_sha256: source.sha256, optimized_file_sha256: targetHash});
    const [sourceBundle, sourceFileHash] = await readBundle(identity, directory, "original", originalText);
    const [targetBundle, fileHash] = await readBundle(identity, directory, "optimized", optimizedText);
    await restoreRepack(sourceBundle, targetBundle, fileHash, serial, `${serial}:${targetHash}`, progress, {live: true, showHashes: false, sourceFileHash}, deps);
  }

  async function installRepack(source, plan, progress) {
    const serial = plan.serial;
    if ((source.capture ?? {}).serial !== serial) throw new Error("The saved backup belongs to a different keyboard.");
    progress("Building and verifying the packed layout on this computer…");
    const [, target, report] = await buildRepack(source, []);
    const after = report.target_layout;
    if (["free_segments", "largest_free_run"].some((key) => after[key] < plan.after[key])) {
      throw runtimeError("The verified plan has less space than the approved preview. No erase was started.");
    }
    await installVerifiedBundle(source, target, serial, progress);
  }

  async function captureApprovedMerge(console, request, progress) {
    const serial = console.serial;
    const upper = get(request, "upper"), lower = get(request, "lower"), tokenValue = get(request, "token");
    if (!serial || get(request, "serial") !== serial || get(request, "confirm") !== serial || !isString(tokenValue) || !/^[0-9a-f]{64}$/.test(tokenValue)
        || [upper, lower].some((identity) => !isInt(identity) || !(identity > 0 && identity <= 0xffffffff))) {
      throw new Error("Confirm a USB merge preview for this keyboard.");
    }
    if (await console.command("native_install update repack-capabilities", 30) !== "OK REPACK1"
        || await console.command("native_install update recovery-capabilities", 30) !== "OK RECOVERY1") {
      throw runtimeError("This keyboard does not offer guarded USB repacking.");
    }
    const source = await backupForRewrite(console, progress);
    const original = await reconstruct(source);
    if (tokenValue !== await offlineMergeToken(original.snapshot, serial, upper, lower)) {
      throw new Error("The dictionaries changed after the preview. No erase was started. Preview the merge again.");
    }
    return [source, original];
  }

  async function installOfflineMerge(source, original, request, progress) {
    progress("Building and verifying the merged layout on this computer…");
    const [target, name] = await buildMergeRepack(source, original, request.upper, request.lower);
    await installVerifiedBundle(source, target, request.serial, progress);
    return name;
  }

  async function lastRepackBackup(serial) {
    if (!store) return null;
    return store.latest(await backupIdentity(serial));
  }

  async function restoreOriginal(serial, request, progress) {
    if (get(request, "serial") !== serial || get(request, "confirm") !== serial) throw new Error("Confirm the USB serial before restoring the original backup.");
    const directory = planToken(get(request, "token"));
    const identity = await backupIdentity(serial);
    if (directory !== await lastRepackBackup(serial)) throw new Error("Refresh the keyboard to select its latest repack backup.");
    const [source] = await readBundle(identity, directory, "original");
    const [marker] = await readBundle(identity, directory, "attempt");
    if ((source.capture ?? {}).serial !== serial || get(marker, "original_sha256") !== source.sha256) {
      throw new Error("The original repack backup changed or belongs to another keyboard.");
    }
    await recoverUsb(source, serial, serial, progress, {unprovisionedOnly: true}, deps);
  }

  return {previewRepack, captureApprovedRepack, installRepack, captureApprovedMerge, installOfflineMerge, lastRepackBackup, restoreOriginal};
}
