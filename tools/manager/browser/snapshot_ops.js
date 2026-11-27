// Snapshot-level operations on the keyboard, ported from tools/storage/native_snapshot.py (merge_snapshots, clear_personal,
// change_snapshot), native_import_plan.py (named_import) and native_base_update.py (factory_package). Each one reads the catalog
// under a staging lease, plans the update with the (already ported) planners, and either returns a preview with an approval token
// or publishes the staged update. Every path releases the lease with `abort` unless the update was published.
import {runtimeError} from "./errors.js";
import {updateCommand, readSnapshot, readFlash} from "./device_reads.js";
import {publish, abort} from "./publish.js";
import {FACTORY_CAPABILITIES, mergeLimit, need, parseJson} from "./state.js";
import {validateName, rowName, SEGMENT} from "./snapshot.js";
import {compileSnapshot} from "./compile.js";
import {parseOutline} from "./lookup.js";
import {effectiveEntries} from "./decode.js";
import {NoSpace} from "./plan.js";
import {FACTORY_KEYS, planPackage, packageApproval} from "./package_plan.js";
import {planChange, mergeRows, mergedEntries, journalBaseline, inspectFree, planCompactImport, metadataSnapshot, consolidationCost,
  importToken, offlineMergeToken, mergeToken, pyJson, sha256Hex} from "./snapshot_plan.js";

const utf8 = (text) => new TextEncoder().encode(text);
const view = (bytes) => new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
const grouped = (value) => value.toLocaleString("en-US");
const isInt = (value) => typeof value === "number" && Number.isInteger(value);
const toBase = (digits) => (/^[0-9]+$/.test(digits) ? Number(digits) : (() => { throw new Error(`invalid literal for int() with base 10: '${digits}'`); })());

// Spans of the extents a staged catalog payload describes.
function extentSpans(payload) {
  const v = view(payload);
  const rows = v.getUint16(4, true);
  let total = 0;
  for (let at = 32 + 96 * rows; at + 24 <= payload.length; at += 24) total += v.getUint16(at + 2, true);
  return total;
}

export async function mergeSnapshots(console, upperId, lowerId, {expected = null, progress = () => {}} = {}) {
  if ([upperId, lowerId].some((identity) => !isInt(identity) || !(identity > 0 && identity <= 0xffffffff))) throw new Error("Select two compact dictionaries.");
  if (await mergeLimit(console) === null) throw new Error("Compact dictionary merging is unavailable.");
  const answer = await updateCommand(console, `begin snapshot-merge ${upperId} ${lowerId}`);
  let published = false;
  try {
    const {snapshot, protected: protectedSegments} = await readSnapshot(console, answer);
    const [upper, lower] = mergeRows(snapshot, upperId, lowerId);
    const names = [rowName(upper), rowName(lower)];
    progress("Reading both compact dictionaries…");
    const {merged, duplicates} = await mergedEntries(snapshot, upper, lower, (at, size) => readFlash(console, at, size));
    progress("Compiling the merged dictionary…");
    const unique = new Map();
    for (const [outline, text] of merged) { const strokes = parseOutline(outline); unique.set(strokes.join(","), [strokes, utf8(text)]); }
    const compiled = compileSnapshot(names[0], [...unique.values()]);
    const [free, largest] = inspectFree(protectedSegments, snapshot.poolEnd);
    const report = {names, ids: [upperId, lowerId], entries: merged.size, duplicates,
      source_image_segments: [upper, lower].map((row) => snapshot.extents[view(row).getUint16(8, true)][1]),
      image_segments: Math.floor((compiled.data.length + SEGMENT - 1) / SEGMENT), free_segments: free, largest_free_run: largest, reserve: 3};
    let update;
    try {
      update = planChange(snapshot, protectedSegments, upperId, compiled, {consolidate: true, mergeId: lowerId});
    } catch (error) {
      if (!(error instanceof NoSpace)) throw error;
      if (expected !== null) throw error;
      let fallback = {};
      if (console.serial && await console.command("native_install update repack-capabilities", 30) === "OK REPACK1"
          && await console.command("native_install update recovery-capabilities", 30) === "OK RECOVERY1") {
        fallback = {serial: console.serial, offline_token: await offlineMergeToken(snapshot, console.serial, upperId, lowerId)};
      }
      return {...report, token: null, reason: error.message, ...fallback};
    }
    const segmentDelta = extentSpans(update.payload) - snapshot.extents.reduce((sum, extent) => sum + extent[1], 0);
    const token = await mergeToken(snapshot, update, console.serial, answer, upperId, lowerId);
    if (expected === null) return {...report, token, segment_delta: segmentDelta, staging_segments: update.segments.size};
    if (expected !== token) throw new Error("The dictionaries or available space changed. Preview the merge again.");
    await publish(console, update, progress);
    published = true;
    return names[0];
  } finally {
    if (!published) await abort(console);
  }
}

export async function clearPersonal(console, identity, progress) {
  const answer = await updateCommand(console, `begin snapshot-clear ${identity}`);
  let published = false;
  try {
    const {snapshot, protected: protectedSegments} = await readSnapshot(console, answer);
    const update = planChange(snapshot, protectedSegments, identity, null, {clear: true});
    await publish(console, update, progress);
    published = true;
    return true;
  } finally {
    if (!published) await abort(console);
  }
}

export async function changeSnapshot(console, operation, name, {source = null, stage = false, progress = () => {}, preview = false,
    expected = null, target_id: targetId = null} = {}) {
  if (!["personal", "convert", "consolidate", "delete"].includes(operation)
      || ((preview || expected !== null) && (operation === "delete" || (preview && stage)))) throw new Error("Select an optimization to preview or stage.");
  if (await updateCommand(console, "snapshot-capabilities") !== "OK SNAPSHOT2") throw runtimeError("Flash the matching experimental lifecycle firmware");
  validateName(name);
  const converting = operation === "personal" || operation === "convert";
  if (operation === "personal" && await updateCommand(console, "personal-snapshot-capabilities") !== "OK PERSONAL1") throw runtimeError("Flash the experimental personal conversion firmware");
  if (operation === "convert" && await updateCommand(console, "journal-snapshot-capabilities") !== "OK JOURNAL1") throw runtimeError("Update the left firmware to convert named journal dictionaries.");
  const expectedKind = operation === "personal" ? "personal" : operation === "convert" ? "named" : "snapshot";
  const row = parseJson(await console.command("native_catalog", 30)).find((candidate) => need(candidate, "kind") === expectedKind && need(candidate, "name") === name) ?? null;
  if (row === null || (targetId !== null && row.id !== targetId)) throw new Error("The selected dictionary is no longer eligible. Refresh and choose it again.");
  const identity = row.id;
  const answer = await updateCommand(console, `begin snapshot-${operation} ${identity}`);
  let published = false;
  try {
    const consolidating = operation === "consolidate";
    const report = preview || expected !== null;
    const {snapshot, protected: protectedSegments} = await readSnapshot(console, answer, {metadataOnly: !(consolidating || converting || report)});
    const target = snapshot.rows.find((candidate) => rowName(candidate) === name && view(candidate).getUint32(0, true) === identity) ?? null;
    if (target === null) throw runtimeError("Catalog changed before staging. Refresh and retry.");
    // Read generation under the staging lease: a textually identical plan from a different generation or keyboard needs its own approval.
    const generation = report ? need(parseJson(await console.command("user_dictionary_status", 30)), "generation") : null;
    let beforeBytes = 0, edits = [];
    if (report) {
      const base = view(target).getUint16(8, true);
      const imageBytes = base !== 65535 ? snapshot.extents[base][2] : 0;
      edits = [...snapshot.entries.values()].filter((entry) => entry.identity === identity);
      beforeBytes = imageBytes + edits.reduce((sum, entry) => sum + entry.span, 0);
      if (consolidating && !edits.length && preview) {
        return {token: null, id: identity, name, operation, before_bytes: beforeBytes, after_bytes: beforeBytes, staging_segments: 0, segment_delta: 0};
      }
    }
    let compiled = null;
    if (converting) {
      const baseline = journalBaseline(snapshot, identity, source);
      if (preview && !baseline.length) {
        return {token: null, id: identity, name, operation, before_bytes: beforeBytes, after_bytes: beforeBytes, staging_segments: 0, segment_delta: 0,
          reason: "This journal has no entries to compact."};
      }
      compiled = compileSnapshot(name, baseline);
    } else if (consolidating) {
      const entries = await effectiveEntries(snapshot, name, (at, size) => readFlash(console, at, size));
      const unique = new Map();
      for (const [outline, text] of entries) { const strokes = parseOutline(outline); unique.set(strokes.join(","), [strokes, utf8(text)]); }
      compiled = compileSnapshot(name, [...unique.values()]);
    }
    let free = 0, largest = 0, sectors = 0;
    if (compiled !== null) {
      [free, largest] = inspectFree(protectedSegments, snapshot.poolEnd);
      sectors = Math.floor((compiled.data.length + SEGMENT - 1) / SEGMENT);
      progress(`${grouped(compiled.data.length)} compact bytes · ${sectors} contiguous segments needed · ${free} free, largest run ${largest}, reserve 3`);
    }
    const update = planChange(snapshot, protectedSegments, identity, compiled, {consolidate: consolidating, convert: converting});
    progress(`${operation[0].toUpperCase() + operation.slice(1).toLowerCase()} ${name}: ${update.segments.size} staging segments`);
    if (report) {
      const parts = [utf8(pyJson([console.serial, generation, operation, identity, snapshot.highwater, [...protectedSegments].sort((a, b) => a - b)], [",", ":"])),
        snapshot.payload];
      for (const [segment, block] of [...update.segments].sort((a, b) => a[0] - b[0])) {
        const header = new Uint8Array(8);
        view(header).setUint32(0, segment, true);
        view(header).setUint32(4, block.length, true);
        parts.push(header, block);
      }
      const token = await sha256Hex(...parts);
      if (expected !== null && expected !== token) throw runtimeError("The optimization plan changed. Preview it again before staging.");
      if (preview) {
        const retained = edits.reduce((sum, entry) => sum + (converting && entry.operation === 1 ? entry.span : 0), 0);
        const segmentDelta = extentSpans(update.payload) - snapshot.extents.reduce((sum, extent) => sum + extent[1], 0);
        return {token, id: identity, name, operation, generation, before_bytes: beforeBytes, after_bytes: compiled.data.length + retained,
          compact_bytes: compiled.data.length, image_segments: sectors, staging_segments: update.segments.size, segment_delta: segmentDelta,
          free_segments: free, largest_free_run: largest, reserve: 3};
      }
    }
    if (stage) {
      await publish(console, update, progress);
      published = true;
    }
    return published;
  } finally {
    if (!published) await abort(console);
  }
}

export async function namedImport(console, name, entries, {stage = false, expected = null, progress = () => {}} = {}) {
  validateName(name);
  if (!entries.length) throw new Error("This dictionary has zero entries.");
  if (await updateCommand(console, "snapshot-capabilities") !== "OK SNAPSHOT2") throw runtimeError("This keyboard does not offer compact dictionary imports.");
  const capability = (await updateCommand(console, "named-capabilities")).trim().split(/\s+/);
  if (capability.length !== 3 || capability[0] !== "OK" || capability[1] !== "NAMED1") throw runtimeError("This keyboard does not offer named dictionary imports.");
  const limit = toBase(capability[2]);
  progress("Compiling the compact dictionary…");
  const compiled = compileSnapshot(name, entries);

  const answer = await updateCommand(console, "begin snapshot-named");
  let published = false;
  try {
    progress("Reading the catalog and checking available space…");
    const {snapshot, protected: protectedSegments} = await readSnapshot(console, answer, {metadataOnly: true});
    const status = parseJson(await console.command("user_dictionary_status", 30));
    const generation = status?.generation ?? null, tail = status?.tail ?? null;
    if (!isInt(generation) || !isInt(tail) || !(tail >= 0 && tail <= 256)) throw runtimeError("The keyboard did not report its catalog generation and pending edits.");
    let [report, update] = planCompactImport(snapshot, protectedSegments, name, compiled, limit);
    report.entries = entries.length;
    if (update !== null) {
      const after = await metadataSnapshot(update.payload, snapshot.poolEnd);
      // Each uncheckpointed edit can add at most one record to each index. If this bound cannot fit, check the exact live counts.
      const sizes = new Map();
      for (const extent of snapshot.extents) if (extent[4] === 3 || extent[4] === 4) sizes.set(extent[4], Math.floor(extent[2] / 4));
      try {
        consolidationCost(after, report.image_segments, [3, 4].map((kind) => (sizes.get(kind) ?? 0) + tail));
      } catch (error) {
        if (!(error instanceof NoSpace)) throw error;
        progress("Checking live journal usage for future consolidation…");
        const {snapshot: live} = await readSnapshot(console, answer);
        const counts = [live.entries.size, [...live.entries.values()].filter((entry) => entry.operation === 0).length];
        try {
          consolidationCost(after, report.image_segments, counts);
        } catch (inner) {
          if (!(inner instanceof NoSpace)) throw inner;
          report.reason = `A same-size consolidation could not stage. ${inner.message}`;
          update = null;
        }
      }
    }
    let token = null;
    if (update !== null) token = await importToken(compiled.data, snapshot.payload, console.serial, generation, tail, protectedSegments);
    if (stage) {
      if (expected !== null && expected !== token) throw runtimeError("The import plan changed. Preview it again before staging.");
      if (update === null) throw new NoSpace(report.reason);
      await publish(console, update, progress);
      published = true;
    }
    return {...report, token};
  } finally {
    if (!published) await abort(console);
  }
}

// `prepare(name)` builds the pinned compiled image for a factory dictionary ({contents, pointers, digest}); see factory.js.
export async function factoryPackage(console, pkg, {restore = false, confirmation = null, progress = () => {}} = {}, prepare = async () => {
  throw new Error("Factory dictionaries are not available in the JavaScript Manager yet.");
}) {
  if (!Object.hasOwn(FACTORY_KEYS, pkg)) throw new Error("Select a factory dictionary");
  if (await console.command(FACTORY_CAPABILITIES, 30) !== "OK FACTORY2") throw runtimeError("Update the left firmware to manage factory dictionaries.");
  const [identity, name] = FACTORY_KEYS[pkg];
  const compiled = restore ? await prepare(name) : null;
  const answer = await updateCommand(console, `begin factory-member ${identity}`);
  let published = false;
  try {
    const {snapshot, protected: protectedSegments} = await readSnapshot(console, answer, {metadataOnly: true});
    const {update, report} = await planPackage(snapshot, protectedSegments, pkg, restore, compiled);
    const approval = await packageApproval(console.serial, report.confirmation);
    report.confirmation = approval;
    if (confirmation !== null) {
      if (confirmation !== approval) throw new Error("The approved plan changed. Preview the package again.");
      await publish(console, update, progress);
      published = true;
    }
    return report;
  } finally {
    if (!published) await abort(console);
  }
}
