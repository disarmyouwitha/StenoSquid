// node tools/manager/browser/test_transfer.mjs   (no browser required)
// Gate for publish.js: transfer() must send the same console commands (with the same timeouts and the same Base64 writes) as
// native_update.transfer, report the same progress, return the same result and fail the same way.
import assert from "node:assert/strict";
import fs from "node:fs";
import {transfer} from "./publish.js";

const vectors = JSON.parse(fs.readFileSync(new URL("./golden/transfer_vectors.json", import.meta.url), "utf8"));
const fromHex = (text) => Uint8Array.from(Buffer.from(text, "hex"));

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
  const options = {move: c.options.move ?? null, rename: c.options.rename ?? null, delete: c.options.delete ?? null,
    previewCompact: c.options.preview_compact ?? false, expectedCompact: c.options.expected_compact ?? null};
  if ("expected_compact" in c.options && c.options.expected_compact === null) options.expectedCompact = null;
  const incoming = c.incoming === null ? null : c.incoming.map(([strokes, text]) => [strokes, fromHex(text)]);
  const messages = [];
  let result = null, error = null;
  try { result = await transfer(console_, incoming, (message) => messages.push(message), options); } catch (e) { error = e; }
  if ("error" in c.expect) {
    assert.ok(error, `${c.title}: Python raised ${c.expect.error.message} but JS returned ${JSON.stringify(result)}`);
    if (!/^(not enough values|Only base64|invalid literal)/.test(c.expect.error.message)) assert.equal(error.message, c.expect.error.message, `${c.title}: error text`);
    assert.equal(error.kind === "RuntimeError", c.expect.error.type === "RuntimeError", `${c.title}: RuntimeError or not (${c.expect.error.type})`);
  } else {
    assert.ok(!error, `${c.title}: JS raised ${error?.message}`);
    assert.deepEqual(result, c.expect.result, `${c.title}: result`);
  }
  assert.deepEqual(messages, c.progress, `${c.title}: progress`);
  assert.equal(next, c.log.length, `${c.title}: JS sent ${next} of ${c.log.length} commands`);
}
console.log(`PASS publish and transfer (JS): ${vectors.cases.length} cases behave like native_update.transfer`);
