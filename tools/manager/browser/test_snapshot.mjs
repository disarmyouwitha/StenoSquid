// node tools/manager/browser/test_snapshot.mjs   (no browser required)
// Gate for Manager milestone M1: the JS snapshot reader and metadata planner must agree with the Python code
// (the oracle) on golden vectors from golden_snapshot.py: same parsed state, same error text, same flash reads.
import assert from "node:assert/strict";
import fs from "node:fs";
import {Snapshot, metadataPayload, readRecord, encodeRecord, Edit, SEGMENT} from "./snapshot.js";

const vectors = JSON.parse(fs.readFileSync(new URL("./golden/snapshot_vectors.json", import.meta.url), "utf8"));
const fromHex = (hex) => Uint8Array.from(Buffer.from(hex, "hex"));
const toHex = (bytes) => Buffer.from(bytes).toString("hex");

function replayReader(reads) {
  const log = [];
  let next = 0;
  const read = (address, length) => {
    // The JS reader must make the same reads in the same order as Python.
    const expected = reads[next++];
    assert.ok(expected, `unexpected extra read at ${address}`);
    assert.deepEqual([address, length], [expected.address, expected.length], "read pattern differs from Python");
    log.push(address);
    const out = new Uint8Array(length).fill(0xff);
    out.set(fromHex(expected.hex));
    return out;
  };
  return {read, consumed: () => next};
}

function summary(snapshot, metadataOnly) {
  const out = {
    next_id: snapshot.nextId, personal: snapshot.personal, rows: snapshot.rows.map(toHex),
    extents: snapshot.extents.map((e) => e.map(Number)),
  };
  if (!metadataOnly) {
    out.highwater = Number(snapshot.highwater);
    out.anchor = snapshot.anchor;
    out.used = Object.fromEntries([...snapshot.used].map(([segment, used]) => [String(segment), used]));
    out.entries = [...snapshot.entries.values()].map((e) => [e.identity, e.strokes, e.address, e.operation, toHex(e.text)]);
  }
  return out;
}

let checked = 0, metadataChecked = 0;
for (const c of vectors.cases) {
  const {read, consumed} = replayReader(c.reads);
  const options = {strict: c.options.strict, metadataOnly: c.options.metadataOnly, poolEnd: c.options.poolEnd};
  let snapshot = null, error = null;
  try {
    snapshot = await Snapshot.load(read, c.catalog.address, c.catalog.bytes, c.catalog.crc, options);
  } catch (e) { error = e; }
  if ("error" in c.expect) {
    assert.ok(error, `${c.name}: Python rejected this input but JS accepted it`);
    assert.equal(error.message, c.expect.error, `${c.name}: error text`);
  } else {
    assert.ok(!error, `${c.name}: JS rejected an input Python accepted: ${error?.message}`);
    assert.deepEqual(summary(snapshot, c.options.metadataOnly), c.expect, `${c.name}: parsed state`);
  }
  assert.equal(consumed(), c.reads.length, `${c.name}: JS made fewer reads than Python`);
  checked++;

  for (const m of c.metadata_cases ?? []) {
    const args = m.op === "delete" ? {delete: m.args} : {[m.op]: m.args};
    if ("error" in m.expect) {
      assert.throws(() => metadataPayload(snapshot, args), {message: m.expect.error}, `${m.op} ${m.args}`);
    } else {
      const payload = metadataPayload(snapshot, args);
      assert.equal(payload === null ? null : toHex(payload), m.expect.payload, `${m.op} ${m.args}`);
    }
    metadataChecked++;
  }
}

// Record codec: encode then decode returns the same edit, and damage is rejected.
const edit = new Edit(0, 5, 0, [0x10, 0x20], new TextEncoder().encode("héllo"));
const blob = encodeRecord(edit, 42);
const segment = new Uint8Array(SEGMENT).fill(0xff);
segment.set(blob, 256);
const {edit: decoded, sequence} = readRecord(segment, 256, 0);
assert.equal(sequence, 42n);
assert.deepEqual([decoded.identity, decoded.operation, decoded.strokes, toHex(decoded.text)], [5, 0, [0x10, 0x20], toHex(edit.text)]);
assert.equal(decoded.span, (blob.length + 3) & ~3); // records are padded to 4 bytes
segment[256 + 40] ^= 1;
assert.throws(() => readRecord(segment, 256, 0), {message: "Invalid staged-source record checksum"});

console.log(`PASS snapshot reader (JS): ${checked} golden cases, ${metadataChecked} metadata plans`);
