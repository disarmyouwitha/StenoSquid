// node tools/manager/browser/test_state.mjs   (no browser required)
// Gate for Manager milestone M3 (state): state.js must ask the keyboard the same questions, with the same timeouts, in the same
// order as manager.read_state / load_view / installer_view and overlay_usage, and reach the same result or failure, progress
// messages, intermediate views and waits. golden_state.py supplies the transcripts.
import assert from "node:assert/strict";
import fs from "node:fs";
import {installerView, loadView, overlayUsage, readState} from "./state.js";

const vectors = JSON.parse(fs.readFileSync(new URL("./golden/state_vectors.json", import.meta.url), "utf8"));
const NAMES = {TimeoutError: "TimeoutError", KeyError: "KeyError", AssertionError: "AssertionError", JSONDecodeError: "JSONDecodeError"};
const roundTrip = (value) => JSON.parse(JSON.stringify(value));

function replyOf(entry) {
  if (entry.reply && typeof entry.reply === "object") {
    const data = new Uint8Array(entry.reply.count).fill(0xff);
    data.set(Buffer.from(entry.reply.read, "hex"));
    return "OK " + Buffer.from(data).toString("base64");
  }
  return entry.reply;
}

for (const c of vectors.cases) {
  let next = 0;
  const console_ = {
    serial: c.serial,
    transport: c.transport,
    async command(text, timeout = 30) {
      assert.ok(next < c.transcript.length, `${c.title}: extra command ${text}`);
      const entry = c.transcript[next++];
      assert.equal(text, entry.command, `${c.title}: command ${next - 1} differs from Python`);
      assert.equal(timeout, entry.timeout, `${c.title}: timeout of ${text}`);
      if (entry.raise) throw Object.assign(new Error(entry.raise.message), {name: NAMES[entry.raise.type] ?? "Error"});
      return replyOf(entry);
    },
  };
  const sleeps = [], progress = [], shown = [];
  const deps = {lastRepackBackup: async () => c.backup, sleep: async (seconds) => { sleeps.push(seconds); }};
  let actual;
  try {
    let value;
    if (c.fn === "readState") value = await readState(console_, deps);
    else if (c.fn === "installerView") value = await installerView(console_, c.status, {reprovision: c.reprovision, transport: c.transport});
    else if (c.fn === "loadView") value = await loadView(console_, c.action, (m) => progress.push(m), (v) => shown.push(roundTrip(v)), deps);
    else value = await overlayUsage(console_, {metadataOnly: c.metadataOnly});
    actual = {value: roundTrip(value)};
  } catch (error) {
    actual = {error};
  }
  const expected = c.expect;
  if ("error" in expected) {
    assert.ok(actual.error, `${c.title}: Python raised ${expected.error.type} (${expected.error.message}) but JS returned a result`);
    const kind = NAMES[expected.error.type] ?? "Error";
    assert.equal(actual.error.name, kind, `${c.title}: kind of failure (${actual.error.message})`);
    if (expected.error.type !== "JSONDecodeError") assert.equal(actual.error.message, expected.error.message, `${c.title}: error text`);
  } else {
    assert.ok(!actual.error, `${c.title}: JS raised ${actual.error?.name}: ${actual.error?.message} but Python returned a result`);
    assert.deepEqual(actual.value, expected.value, `${c.title}: result`);
  }
  assert.equal(next, c.transcript.length, `${c.title}: JS sent ${next} commands, Python sent ${c.transcript.length}`);
  if (c.fn === "loadView") {
    assert.deepEqual(progress, c.progress, `${c.title}: progress messages`);
    assert.deepEqual(shown, c.shown, `${c.title}: views shown`);
    assert.deepEqual(sleeps, c.sleeps, `${c.title}: waits`);
  }
}
console.log(`PASS keyboard state reads (JS): ${vectors.cases.length} transcripts behave like manager.py`);
