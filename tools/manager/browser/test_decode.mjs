// node tools/manager/browser/test_decode.mjs   (no browser required)
// Gate for Manager milestone M1 (lookup/verify reader): decode.js must return the same entries as Python, and reject
// every damaged or truncated image that Python rejects (same message where Python wrote one), and accept the rest.
import assert from "node:assert/strict";
import {createHash} from "node:crypto";
import fs from "node:fs";
import {Snapshot} from "./snapshot.js";
import {compactEntries, requireSnapshotHint, effectiveEntries} from "./decode.js";

const vectors = JSON.parse(fs.readFileSync(new URL("./golden/decode_vectors.json", import.meta.url), "utf8"));
const fromHex = (hex) => Uint8Array.from(Buffer.from(hex, "hex"));
const digest = (entries) => createHash("sha256").update(entries.map(([o, t]) => `${o}\t${t}`).join("\n"), "utf8").digest("hex");

// Python's own interpreter messages (struct.error, UnicodeDecodeError, bare asserts) are not reproduced; every message
// the code itself wrote must match exactly.
const comparable = ({type, message}) => message !== "" && !["error", "UnicodeDecodeError", "IndexError", "ValueError"].includes(type) ||
  /^(Unsupported compact snapshot|No installed dictionary|Compiled dictionary checksum)/.test(message);

function expectOutcome(label, run, expect) {
  let entries = null, error = null;
  try { entries = run(); } catch (e) { error = e; }
  if ("error" in expect) {
    assert.ok(error, `${label}: Python rejected this input (${expect.error.type}: ${expect.error.message}) but JS accepted it`);
    if (comparable(expect.error)) assert.equal(error.message, expect.error.message, `${label}: error text`);
  } else {
    assert.ok(!error, `${label}: JS rejected an input Python accepted: ${error?.message}`);
    if ("entries" in expect) assert.deepEqual(entries, expect.entries, label);
    else assert.deepEqual({count: entries.length, sha256: digest(entries)}, expect, label);
  }
}

const blobs = Object.fromEntries(Object.entries(vectors.decode.blobs).map(([name, hex]) => [name, fromHex(hex)]));
let decoded = 0;
for (const c of vectors.decode.cases) {
  let blob = Uint8Array.from(blobs[c.blob]);
  for (const [offset, mask] of c.flips) blob[offset] ^= mask;
  if (c.cut !== null) blob = blob.slice(0, c.cut);
  expectOutcome(c.name, () => compactEntries(blob, c.base, c.member, c.count), c.expect);
  decoded++;
}

const snapshotBlob = fromHex(vectors.hint.blob);
let hints = 0;
for (const c of vectors.hint.cases) {
  let blob = Uint8Array.from(snapshotBlob);
  for (const [offset, mask] of c.flips) blob[offset] ^= mask;
  if (c.cut !== null) blob = blob.slice(0, c.cut);
  const expect = c.expect.ok ? {entries: []} : c.expect;
  expectOutcome(c.name, () => { requireSnapshotHint(blob, c.base); return []; }, expect);
  hints++;
}

let effective = 0;
for (const c of vectors.effective.cases) {
  let next = 0;
  const read = (address, length) => {
    const expected = c.reads[next++];
    assert.ok(expected, `${c.name}: extra read`);
    assert.deepEqual([address, length], [expected.address, expected.length], `${c.name}: read pattern differs from Python`);
    const out = new Uint8Array(length).fill(0xff);
    out.set(fromHex(expected.hex));
    return out;
  };
  let entries = null, error = null;
  try {
    const snapshot = await Snapshot.load(read, c.catalog.address, c.catalog.bytes, c.catalog.crc, {poolEnd: 255});
    entries = [...(await effectiveEntries(snapshot, c.target, read))];
  } catch (e) { error = e; }
  if ("error" in c.expect) {
    assert.ok(error, `${c.name}: Python raised but JS did not`);
    if (comparable(c.expect.error)) assert.equal(error.message, c.expect.error.message, `${c.name}: error text`);
  } else {
    assert.ok(!error, `${c.name}: JS raised ${error?.message}`);
    assert.deepEqual(entries, c.expect.entries, c.name);
  }
  assert.equal(next, c.reads.length, `${c.name}: JS made fewer reads than Python`);
  effective++;
}
console.log(`PASS dictionary decoder (JS): ${decoded} decode, ${hints} snapshot-hint, ${effective} effective-entries cases`);
