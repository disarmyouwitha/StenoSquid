// node tools/manager/browser/test_replace.mjs   (no browser required)
// Gate for Manager milestones M1/M2: lookup.js must give the same hits, misses and errors as jsc4.reader, and
// replace_plan.js must give byte-identical updates, changed lists and conflict lists to plan_replacements.
import assert from "node:assert/strict";
import {createHash} from "node:crypto";
import fs from "node:fs";
import {Snapshot} from "./snapshot.js";
import {NoSpace} from "./plan.js";
import {reader} from "./lookup.js";
import {planReplacements} from "./replace_plan.js";

const vectors = JSON.parse(fs.readFileSync(new URL("./golden/replace_vectors.json", import.meta.url), "utf8"));
const toHex = (bytes) => Buffer.from(bytes).toString("hex");
const fromHex = (hex) => Uint8Array.from(Buffer.from(hex, "hex"));

// Messages Python's own runtime wrote (struct.error, UnicodeDecodeError, bare asserts) are not reproduced; every message the
// code itself wrote must match exactly.
const comparable = (type, message) => message !== "" && !["error", "UnicodeDecodeError", "IndexError"].includes(type);
function sameFailure(error, expectType, expectMessage, label) {
  assert.ok(error, `${label}: Python raised ${expectType}: ${expectMessage} but JS did not`);
  if (comparable(expectType, expectMessage)) assert.equal(error.message, expectMessage, `${label}: error text`);
}

// ---- lookup ----
const blobs = Object.fromEntries(Object.entries(vectors.lookup.blobs).map(([name, hex]) => [name, fromHex(hex)]));
let lookupCases = 0, lookups = 0;
for (const c of vectors.lookup.cases) {
  const blob = Uint8Array.from(blobs[c.blob]);
  for (const [offset, mask] of c.flips) blob[offset] ^= mask;
  let lookup = null, error = null;
  try { lookup = reader(blob, c.base, c.member, c.count); } catch (e) { error = e; }
  if ("error" in c.expect) {
    sameFailure(error, c.expect.error.type, c.expect.error.message, c.name);
  } else {
    assert.ok(!error, `${c.name}: JS rejected a reader Python accepted: ${error?.message}`);
    c.outlines.forEach((outline, i) => {
      const expected = c.expect.results[i];
      let value, failure = null;
      try { value = lookup(outline); } catch (e) { failure = e; }
      if ("error" in expected) sameFailure(failure, expected.type, expected.error, `${c.name}: ${JSON.stringify(outline)}`);
      else {
        assert.ok(!failure, `${c.name}: ${JSON.stringify(outline)}: JS raised ${failure?.message}`);
        assert.equal(value, expected.value, `${c.name}: ${JSON.stringify(outline)}`);
      }
      lookups++;
    });
  }
  lookupCases++;
}

// ---- replacement ----
const snapshots = {};
const replaying = (reads) => {
  let next = 0;
  return {
    read(address, length) {
      const expected = reads[next++];
      assert.ok(expected, "extra flash read");
      assert.deepEqual([address, length], [expected.address, expected.length], "read pattern differs from Python");
      const out = new Uint8Array(length).fill(0xff);
      out.set(fromHex(expected.hex));
      return out;
    },
    consumed: () => next,
  };
};
for (const [name, image] of Object.entries(vectors.replace.images)) {
  const {read} = replaying(image.reads);
  snapshots[name] = await Snapshot.load(read, image.catalog.address, image.catalog.bytes, image.catalog.crc, {poolEnd: image.poolEnd});
}

let replaced = 0, updates = 0;
for (const c of vectors.replace.cases) {
  const {read, consumed} = replaying(c.reads);
  const compiled = new Map(Object.entries(c.compiled).map(([name, s]) => [name, {contents: fromHex(s.contents), pointers: s.pointers, digest: fromHex(s.digest)}]));
  let actual;
  try {
    const snapshot = await Snapshot.load(read, c.catalog.address, c.catalog.bytes, c.catalog.crc, {poolEnd: 255});
    const {update, changed, conflicts} = await planReplacements(snapshot, new Set(c.protected), compiled, c.readOriginal ? read : null);
    actual = {value: {
      update: update === null ? null : {metadata: update.metadata, payload: toHex(update.payload),
        segments: [...update.segments].map(([segment, block]) => [segment, block.length, createHash("sha256").update(block).digest("hex")])},
      changed, conflicts,
    }};
    if (update) updates++;
  } catch (error) {
    actual = {error: error.message, type: error instanceof NoSpace ? "NoSpace" : "ValueError"};
  }
  assert.deepEqual(actual, c.expect, c.title);
  assert.equal(consumed(), c.reads.length, `${c.title}: JS made fewer reads than Python`);
  replaced++;
}
console.log(`PASS lookup reader + base replacement (JS): ${lookupCases} lookup cases (${lookups} lookups), ${replaced} replacement cases (${updates} produced an update), byte-identical to Python`);
