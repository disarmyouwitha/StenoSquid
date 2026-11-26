// node tools/manager/browser/test_snapshot_plan.mjs   (no browser required)
// Gate for Manager milestone M2 (named snapshot, change/merge and import planners): snapshot_plan.js must be byte-identical
// to Python's native_snapshot.py / native_import_plan.py on golden vectors, including every error, the token digests and
// the merged-entry computation.
import assert from "node:assert/strict";
import {createHash} from "node:crypto";
import fs from "node:fs";
import {Snapshot} from "./snapshot.js";
import {NoSpace} from "./plan.js";
import {
  CompactImage, planNamedSnapshot, planChange, mergeRows, mergedEntries, journalBaseline, personalBaseline, inspectFree,
  stageAllocator, metadataSnapshot, consolidationCost, planCompactImport, importToken, offlineMergeToken, mergeToken,
} from "./snapshot_plan.js";

const vectors = JSON.parse(fs.readFileSync(new URL("./golden/snapshot_plan_vectors.json", import.meta.url), "utf8"));
const toHex = (bytes) => Buffer.from(bytes).toString("hex");
const fromHex = (hex) => Uint8Array.from(Buffer.from(hex, "hex"));

function buildCompiled(spec) {
  const data = new Uint8Array(spec.length);
  for (let i = 0; i < data.length; i++) data[i] = (i * 7 + 3) % 251;
  const view = new DataView(data.buffer);
  for (const offset of spec.pointers) view.setUint32(offset, (offset * 13 + 8) % spec.length, true);
  if (spec.pointerDelta !== null) view.setUint32(spec.pointers[0], spec.length + spec.pointerDelta, true);
  return new CompactImage(data, [...spec.pointers]);
}

function replayReader(reads) {
  let next = 0;
  const read = (address, length) => {
    const expected = reads[next++];
    assert.ok(expected, "extra flash read");
    assert.deepEqual([address, length], [expected.address, expected.length], "read pattern differs from Python");
    const out = new Uint8Array(length).fill(0xff);
    out.set(fromHex(expected.hex));
    return out;
  };
  return {read, consumed: () => next};
}

const snapshots = {};
for (const [name, image] of Object.entries(vectors.images)) {
  const {read} = replayReader(image.reads);
  snapshots[name] = await Snapshot.load(read, image.catalog.address, image.catalog.bytes, image.catalog.crc, {poolEnd: image.poolEnd});
}

const serialise = (result) => result === null ? null : {
  metadata: result.metadata, payload: toHex(result.payload),
  segments: [...result.segments].map(([segment, block]) => [segment, block.length, createHash("sha256").update(block).digest("hex")]),
};

async function outcome(run) {
  try { return {value: await run()}; } catch (error) {
    return {error: error.message, kind: error instanceof NoSpace ? "NoSpace" : "ValueError"};
  }
}
const sortedProtected = (c) => new Set(c.protected);
const strokesJson = (pairs) => pairs.map(([strokes, text]) => [strokes, toHex(text)]);
const rowId = (snapshot, name) => snapshot.rows.map((row) => [new DataView(row.buffer, row.byteOffset).getUint32(0, true), new TextDecoder().decode(row.subarray(16, 16 + row[12]))]).find(([, n]) => n === name)[0];

let checked = 0;
for (const c of vectors.cases) {
  const snapshot = snapshots[c.image];
  let actual;
  switch (c.kind) {
    case "named":
      actual = await outcome(() => serialise(planNamedSnapshot(snapshot, sortedProtected(c), c.name, buildCompiled(c.compiled), c.limit)));
      break;
    case "change": {
      const flags = {consolidate: c.flags.consolidate ?? false, convert: c.flags.convert ?? false, clear: c.flags.clear ?? false, mergeId: c.flags.merge_id ?? null};
      actual = await outcome(() => serialise(planChange(snapshot, sortedProtected(c), c.identity, c.compiled === null ? null : buildCompiled(c.compiled), flags)));
      break;
    }
    case "mergeRows":
      actual = await outcome(() => { const [a, b] = mergeRows(snapshot, c.upper, c.lower); return [snapshot.rows.indexOf(a), snapshot.rows.indexOf(b)]; });
      break;
    case "baseline": {
      const additions = c.additions === null ? null : c.additions.map(([strokes, hex]) => [strokes, fromHex(hex)]);
      const result = c.who === "personal" ? personalBaseline(snapshot, additions) : journalBaseline(snapshot, rowId(snapshot, c.who), additions);
      actual = strokesJson(result);
      break;
    }
    case "inspectFree": actual = inspectFree(new Set(c.protected), c.poolEnd); break;
    case "allocator": {
      const allocate = stageAllocator(new Set(c.protected), c.poolEnd);
      actual = await outcome(() => [allocate(c.count), allocate(c.count)]);
      break;
    }
    case "importReport":
      actual = await outcome(() => {
        const [report, result] = planCompactImport(snapshot, sortedProtected(c), "Imported Snap", buildCompiled(c.compiled), c.limit);
        return {report, update: serialise(result)};
      });
      break;
    case "consolidationCost": {
      const owned = new Set(vectors.images[c.image].protected);
      const probe = planNamedSnapshot(snapshot, owned, "Cost Probe", buildCompiled(c.compiled), c.limit);
      const after = await metadataSnapshot(probe.payload, snapshot.poolEnd);
      actual = await outcome(() => consolidationCost(after, c.imageSegments, c.indexRecords));
      break;
    }
    case "costDirect":
      actual = await outcome(() => consolidationCost(snapshot, c.imageSegments, c.indexRecords));
      break;
    case "importToken":
      actual = await importToken(buildCompiled(c.compiled).data, snapshot.payload, c.serial, c.generation, c.tail, sortedProtected(c));
      break;
    case "offlineToken": actual = await offlineMergeToken(snapshot, c.serial, c.upper, c.lower); break;
    case "mergeToken": {
      const ids = {upper: c.upper, lower: c.lower};
      const result = planChange(snapshot, sortedProtected(c), ids.upper, buildCompiled(c.compiled), {consolidate: true, mergeId: ids.lower});
      actual = await mergeToken(snapshot, result, c.serial, c.answer, c.upper, c.lower);
      if (c.stagedOrder) assert.deepEqual([...result.segments.keys()], c.stagedOrder, "staging order differs from Python");
      break;
    }
    default: throw new Error("unknown case kind " + c.kind);
  }
  assert.deepEqual(actual, c.expect, `${c.kind}: ${c.name ?? c.title ?? ""}`);
  checked++;
}

// merged_entries replays flash reads against a device-shaped image.
let merged = 0;
for (const c of vectors.merged) {
  const {read, consumed} = replayReader(c.reads);
  let actual;
  try {
    const snapshot = await Snapshot.load(read, c.catalog.address, c.catalog.bytes, c.catalog.crc, {poolEnd: 255});
    const find = (name) => { const row = snapshot.rows.find((r) => new TextDecoder().decode(r.subarray(16, 16 + r[12])) === name); if (!row) throw new Error(`No installed dictionary named '${name}'`); return row; };
    const result = await mergedEntries(snapshot, find(c.upper), find(c.lower), read);
    actual = {entries: [...result.merged], duplicates: result.duplicates};
  } catch (error) { actual = {error: error.message}; }
  assert.deepEqual(actual, c.expect, c.title);
  assert.equal(consumed(), c.reads.length, `${c.title}: JS made fewer reads than Python`);
  merged++;
}
console.log(`PASS snapshot/import planners (JS): ${checked} golden cases + ${merged} merged-entry cases, byte-identical to Python`);
