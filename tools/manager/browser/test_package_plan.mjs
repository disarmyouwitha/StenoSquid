// node tools/manager/browser/test_package_plan.mjs   (no browser required)
// Gate for Manager milestone M2 (factory packages): package_plan.js must give byte-identical staged segments, catalog
// payload, report and confirmation hash to Python's plan_package, or fail with the same error.
import assert from "node:assert/strict";
import {createHash} from "node:crypto";
import fs from "node:fs";
import {Snapshot} from "./snapshot.js";
import {NoSpace} from "./plan.js";
import {planPackage, packageApproval} from "./package_plan.js";

const vectors = JSON.parse(fs.readFileSync(new URL("./golden/package_vectors.json", import.meta.url), "utf8"));
const toHex = (bytes) => Buffer.from(bytes).toString("hex");
const fromHex = (hex) => Uint8Array.from(Buffer.from(hex, "hex"));

function buildCompiled(item) {
  const data = new Uint8Array(item.length);
  for (let i = 0; i < data.length; i++) data[i] = (i * 7 + 3) % 251;
  const view = new DataView(data.buffer);
  for (const offset of item.pointers) view.setUint32(offset, (offset * 13 + 8) % item.length, true);
  if (item.pointerDelta !== null) view.setUint32(item.pointers[0], item.length + item.pointerDelta, true);
  return {contents: data, pointers: [...item.pointers], digest: fromHex(item.digest)};
}

const snapshots = {};
for (const [name, image] of Object.entries(vectors.images)) {
  let next = 0;
  const read = (address, length) => {
    const expected = image.reads[next++];
    assert.deepEqual([address, length], [expected.address, expected.length], "snapshot read pattern differs from Python");
    const out = new Uint8Array(length).fill(0xff);
    out.set(fromHex(expected.hex));
    return out;
  };
  snapshots[name] = await Snapshot.load(read, image.catalog.address, image.catalog.bytes, image.catalog.crc, {poolEnd: image.poolEnd});
}

let checked = 0, produced = 0;
for (const c of vectors.cases) {
  let actual;
  if (c.kind === "approval") {
    actual = await packageApproval(c.serial, c.confirmation);
    assert.equal(actual, c.expect, "approval hash");
  } else {
    try {
      const {update, report} = await planPackage(snapshots[c.image], new Set(c.protected), c.package, c.restore, c.compiled === null ? null : buildCompiled(c.compiled));
      actual = {value: {
        metadata: update.metadata, payload: toHex(update.payload), report,
        segments: [...update.segments].map(([segment, block]) => [segment, block.length, createHash("sha256").update(block).digest("hex")]),
      }};
      produced++;
    } catch (error) {
      actual = {error: error.message, kind: error instanceof NoSpace ? "NoSpace" : "ValueError"};
    }
    assert.deepEqual(actual, c.expect, c.title);
  }
  checked++;
}
console.log(`PASS factory-package planner (JS): ${checked} golden cases (${produced} produced an update), byte-identical to Python`);
