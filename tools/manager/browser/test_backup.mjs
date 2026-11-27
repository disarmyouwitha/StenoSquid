// node tools/manager/browser/test_backup.mjs   (no browser required)
// Gate for backupAll: same console commands (and timeouts) in the same order as native_update.backup_all, same progress calls,
// same backup byte for byte (the capture timestamp is fixed), same failures.
import assert from "node:assert/strict";
import fs from "node:fs";
import {backupAll, isoNow} from "./backup.js";

const vectors = JSON.parse(fs.readFileSync(new URL("./golden/backup_vectors.json", import.meta.url), "utf8"));

for (const c of vectors.cases) {
  let next = 0;
  const console_ = {
    serial: c.serial,
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
  const calls = [];
  let result = null, error = null;
  try {
    result = await backupAll(console_, {onProgress: c.progress ? (received, total) => calls.push([received, total]) : null, now: () => vectors.fixed});
  } catch (e) { error = e; }
  if ("error" in c.expect) {
    assert.ok(error, `${c.title}: Python raised ${c.expect.error.message} but JS made a backup`);
    // Interpreter text (unpacking, base64) is not a contract; the port's own messages are, and so is RuntimeError-ness.
    if (!/^(not enough values|Only base64|Incorrect padding|invalid literal)/.test(c.expect.error.message)) assert.equal(error.message, c.expect.error.message, `${c.title}: error text`);
    assert.equal(error.kind === "RuntimeError", c.expect.error.type === "RuntimeError", `${c.title}: RuntimeError or not (${c.expect.error.type})`);
  } else {
    assert.ok(!error, `${c.title}: JS raised ${error?.message}`);
    assert.deepEqual(result, c.expect.backup, `${c.title}: the backup`);
  }
  assert.deepEqual(calls, c.calls, `${c.title}: progress calls`);
  assert.equal(next, c.log.length, `${c.title}: JS sent ${next} of ${c.log.length} commands`);
}

// The real timestamp has Python's isoformat shape: seconds, optional six-digit microseconds, +00:00.
for (let i = 0; i < 50; i++) assert.match(isoNow(), /^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d{6})?\+00:00$/);
console.log(`PASS backup (JS): ${vectors.cases.length} cases behave like native_update.backup_all`);
