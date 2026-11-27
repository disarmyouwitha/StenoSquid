// node tools/manager/browser/test_snapshot_ops.mjs   (no browser required)
// Gate for snapshot_ops.js: merge_snapshots, clear_personal, change_snapshot, named_import and factory_package must send the same
// console commands (same timeouts, same staged bytes), report the same progress and return or raise the same things as Python.
import assert from "node:assert/strict";
import fs from "node:fs";
import {mergeSnapshots, clearPersonal, changeSnapshot, namedImport, factoryPackage} from "./snapshot_ops.js";
import {parseOutline} from "./lookup.js";

const vectors = JSON.parse(fs.readFileSync(new URL("./golden/snapshot_ops_vectors.json", import.meta.url), "utf8"));
const fromHex = (text) => Uint8Array.from(Buffer.from(text, "hex"));

// The patterned compiled image of golden_package.build_compiled.
function compiledFrom(spec, name) {
  const {length, pointers, pointerDelta} = spec;
  const data = new Uint8Array(length);
  for (let i = 0; i < length; i++) data[i] = (i * 7 + 3) % 251;
  const view = new DataView(data.buffer);
  for (const offset of pointers) view.setUint32(offset, (offset * 13 + 8) % length, true);
  if (pointerDelta !== null) view.setUint32(pointers[0], length + pointerDelta, true);
  return {contents: data, pointers: [...pointers], digest: fromHex(spec.digest)};
}

for (const c of vectors.cases) {
  let next = 0;
  const console_ = {
    serial: "AB12",
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
  const messages = [], progress = (message) => messages.push(message), a = c.args;
  let result = null, error = null;
  try {
    if (c.fn === "mergeSnapshots") result = await mergeSnapshots(console_, a.upper, a.lower, {expected: a.expected ?? null, progress});
    else if (c.fn === "clearPersonal") result = await clearPersonal(console_, a.identity, progress);
    else if (c.fn === "changeSnapshot") {
      result = await changeSnapshot(console_, a.operation, a.name, {stage: a.stage ?? false, preview: a.preview ?? false, expected: a.expected ?? null,
        target_id: a.target_id ?? null, progress});
    } else if (c.fn === "namedImport") {
      const entries = a.entries.map(([key, text]) => [parseOutline(key), fromHex(text)]);
      result = await namedImport(console_, a.name, entries, {stage: a.stage ?? false, expected: a.expected ?? null, progress});
    } else if (c.fn === "factoryPackage") {
      result = await factoryPackage(console_, a.package, {restore: a.restore, confirmation: a.confirmation ?? null, progress},
        async (name) => compiledFrom(c.prepare, name));
    } else assert.fail(`${c.title}: unknown operation ${c.fn}`);
  } catch (e) { error = e; }
  if ("error" in c.expect) {
    assert.ok(error, `${c.title}: Python raised ${c.expect.error.message} but JS returned ${JSON.stringify(result)}`);
    if (!/^(not enough values|Only base64|invalid literal)/.test(c.expect.error.message)) assert.equal(error.message, c.expect.error.message, `${c.title}: error text`);
    assert.equal(error.kind === "RuntimeError", c.expect.error.type === "RuntimeError", `${c.title}: RuntimeError or not (${c.expect.error.type})`);
  } else {
    assert.ok(!error, `${c.title}: JS raised ${error?.message}`);
    assert.deepEqual(JSON.parse(JSON.stringify(result)), c.expect.result, `${c.title}: result`);
  }
  assert.deepEqual(messages, c.progress, `${c.title}: progress`);
  assert.equal(next, c.log.length, `${c.title}: JS sent ${next} of ${c.log.length} commands`);
}
console.log(`PASS snapshot operations (JS): ${vectors.cases.length} cases behave like native_snapshot, native_import_plan and native_base_update`);
