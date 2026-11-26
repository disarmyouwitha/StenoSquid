// node tools/manager/browser/test_device_reads.mjs   (no browser required)
// Gate for Manager milestone M1 (console reads): the JS readSnapshot / readFlash / readLiveJournal must send the same
// console commands in the same order as Python's read_snapshot, and reach the same state, error and byte count.
import assert from "node:assert/strict";
import fs from "node:fs";
import {readSnapshot} from "./device_reads.js";

const vectors = JSON.parse(fs.readFileSync(new URL("./golden/device_read_vectors.json", import.meta.url), "utf8"));
const toHex = (bytes) => Buffer.from(bytes).toString("hex");

function fakeConsole(c) {
  let next = 0;
  return {
    consumed: () => next,
    async command(text) {
      assert.ok(next < c.commands.length, `${c.name}: extra command ${text}`);
      assert.equal(text, c.commands[next], `${c.name}: command ${next} differs from Python`);
      const count = Number(text.split(" ").at(-1));
      const data = new Uint8Array(count).fill(0xff);
      data.set(Buffer.from(c.reads[next], "hex"));
      next++;
      if (c.mode === "busy") return "ERR Native flash busy";
      return "OK " + Buffer.from(c.mode === "short" ? data.subarray(0, count - 1) : data).toString("base64");
    },
  };
}

function summary(snapshot, metadataOnly) {
  const out = {next_id: snapshot.nextId, personal: snapshot.personal, rows: snapshot.rows.map(toHex), extents: snapshot.extents.map((e) => e.map(Number))};
  if (!metadataOnly) {
    out.highwater = Number(snapshot.highwater);
    out.anchor = snapshot.anchor;
    out.used = Object.fromEntries([...snapshot.used].map(([segment, used]) => [String(segment), used]));
    out.entries = [...snapshot.entries.values()].map((e) => [e.identity, e.strokes, e.address, e.operation, toHex(e.text)]);
  }
  return out;
}

let checked = 0;
for (const c of vectors.cases) {
  const console_ = fakeConsole(c);
  let bytes = 0, result = null, error = null;
  try {
    result = await readSnapshot(console_, c.answer, {metadataOnly: c.options.metadataOnly, trimJournals: c.options.trimJournals, onRead: (n) => { bytes += n; }});
  } catch (e) { error = e; }
  if ("error" in c.expect) {
    assert.ok(error, `${c.name}: Python raised but JS did not`);
    // Messages produced by Python's own unpacking/int() are interpreter text; the others must match exactly.
    if (!/^(not enough values|invalid literal)/.test(c.expect.error)) assert.equal(error.message, c.expect.error, `${c.name}: error text`);
  } else {
    assert.ok(!error, `${c.name}: JS raised ${error?.message}`);
    assert.deepEqual(summary(result.snapshot, c.options.metadataOnly), c.expect.snapshot, `${c.name}: snapshot`);
    assert.deepEqual([...result.protected].sort((a, b) => a - b), c.expect.protected, `${c.name}: protected segments`);
    assert.equal(bytes, c.expect.bytesRead, `${c.name}: bytes reported to onRead`);
  }
  assert.equal(console_.consumed(), c.commands.length, `${c.name}: JS sent fewer commands than Python`);
  checked++;
}
console.log(`PASS console snapshot reads (JS): ${checked} cases`);
