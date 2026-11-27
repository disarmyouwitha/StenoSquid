// node tools/manager/browser/test_export.mjs   (no browser required)
// Gate for exportDictionary: it must send the same console commands (with the same timeouts) as native_update.export_dictionary,
// return the same entries in the same order, report the same bytes, and fail the same way.
import assert from "node:assert/strict";
import fs from "node:fs";
import {exportDictionary} from "./export.js";

const vectors = JSON.parse(fs.readFileSync(new URL("./golden/export_vectors.json", import.meta.url), "utf8"));

for (const c of vectors.cases) {
  let next = 0;
  const console_ = {
    async command(text, timeout = 30) {
      const step = c.log[next++];
      assert.ok(step, `${c.title}: extra command ${text}`);
      assert.equal(text, step.c, `${c.title}: command ${next - 1}`);
      assert.equal(timeout, step.t, `${c.title}: timeout of ${text}`);
      if ("d" in step) {
        const count = Number(text.split(" ").at(-1));
        const data = new Uint8Array(count).fill(0xff);
        data.set(Buffer.from(step.d, "hex"));
        return "OK " + Buffer.from(data).toString("base64");
      }
      return step.r;
    },
  };
  let bytes = 0, hooked = 0, result = null, error = null;
  try {
    result = await exportDictionary(console_, c.name, {onRead: (n) => { bytes += n; }, onSnapshot: c.snapshot_hook ? () => { hooked++; } : null, trimJournals: c.trim});
  } catch (e) { error = e; }
  if ("error" in c.expect) {
    assert.ok(error, `${c.title}: Python raised ${c.expect.error.message} but JS returned entries`);
    // Messages from Python's own base64 / unpacking are interpreter text; everything else must match exactly.
    if (!/^(not enough values|Only base64|invalid literal)/.test(c.expect.error.message)) assert.equal(error.message, c.expect.error.message, `${c.title}: error text`);
  } else {
    assert.ok(!error, `${c.title}: JS raised ${error?.message}`);
    assert.deepEqual([...result.entries()], c.expect.entries, `${c.title}: entries`);
  }
  assert.equal(bytes, c.bytes, `${c.title}: bytes reported`);
  assert.equal(hooked, c.hooked, `${c.title}: snapshot hook calls`);
  assert.equal(next, c.log.length, `${c.title}: JS sent ${next} of ${c.log.length} commands`);
}
console.log(`PASS dictionary export (JS): ${vectors.cases.length} cases behave like native_update.export_dictionary`);
