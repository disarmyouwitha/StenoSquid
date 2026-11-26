// node tools/manager/browser/test_plan.mjs   (no browser required)
// Gate for Manager milestone M2 (update planner): plan.js must produce byte-identical updates to Python's plan()
// (same segments in the same upload order, same catalog payload) or fail with the same error.
import assert from "node:assert/strict";
import {createHash} from "node:crypto";
import fs from "node:fs";
import {Snapshot} from "./snapshot.js";
import {plan, NoSpace} from "./plan.js";

const vectors = JSON.parse(fs.readFileSync(new URL("./golden/plan_vectors.json", import.meta.url), "utf8"));
const toHex = (bytes) => Buffer.from(bytes).toString("hex");
const fromHex = (hex) => Uint8Array.from(Buffer.from(hex, "hex"));

async function load(image) {
  let next = 0;
  const read = (address, length) => {
    const expected = image.reads[next++];
    assert.deepEqual([address, length], [expected.address, expected.length], "snapshot read pattern differs from Python");
    const out = new Uint8Array(length).fill(0xff);
    out.set(fromHex(expected.hex));
    return out;
  };
  return Snapshot.load(read, image.catalog.address, image.catalog.bytes, image.catalog.crc, {poolEnd: image.poolEnd});
}

function serialise(update) {
  if (update === null) return {update: null};
  return {update: {
    metadata: update.metadata, payload: toHex(update.payload), reclaimed: update.reclaimed, changes: update.changes,
    segments: [...update.segments].map(([segment, block]) => [segment, block.length, createHash("sha256").update(block).digest("hex")]),
  }};
}

const snapshots = {};
for (const [name, image] of Object.entries(vectors.images)) snapshots[name] = await load(image);

let checked = 0, produced = 0;
for (const c of vectors.cases) {
  const incoming = c.incoming === null ? null : c.incoming.map(([strokes, text]) => [strokes, fromHex(text)]);
  const {move = null, rename = null, delete: remove = null, name = null, limit = 64} = c.arguments;
  let actual;
  try {
    actual = serialise(plan(snapshots[c.image], new Set(c.protected), incoming, {name, limit, move, rename, delete: remove}));
  } catch (error) {
    actual = {error: error.message, kind: error instanceof NoSpace ? "NoSpace" : "ValueError"};
  }
  assert.deepEqual(actual, c.expect, c.name);
  if (actual.update) produced++;
  checked++;
}
console.log(`PASS update planner (JS): ${checked} golden cases (${produced} produced an update), byte-identical to Python`);
