// node tools/manager/browser/test_import.mjs   (no browser required)
// Gate for Manager milestone M1 (import): importer.js must accept and reject exactly the JSON texts Python's parse_named does,
// return the entries and outline names in the same order, give the same message where the Python code wrote one, and write
// byte-identical export files.
import assert from "node:assert/strict";
import fs from "node:fs";
import {parseNamed, toPloverJson} from "./importer.js";

const vectors = JSON.parse(fs.readFileSync(new URL("./golden/import_vectors.json", import.meta.url), "utf8"));
const toHex = (bytes) => Buffer.from(bytes).toString("hex");
const fromHex = (hex) => Uint8Array.from(Buffer.from(hex, "hex"));

let parsed = 0, refused = 0;
for (const c of vectors.parse) {
  let result = null, error = null;
  try { result = parseNamed(c.source); } catch (e) { error = e; }
  if ("error" in c.expect) {
    assert.ok(error, `${c.title}: Python refused this (${c.expect.error.type}: ${c.expect.error.message}) but JS accepted it`);
    // Messages Python's json module and codecs wrote are not reproduced; ValueErrors written by the importer must match.
    if (c.expect.error.type === "ValueError") assert.equal(error.message, c.expect.error.message, `${c.title}: error text`);
    else assert.equal(error.name, c.expect.error.type, `${c.title}: kind of failure (syntax, encoding or semantic)`);
    refused++;
  } else {
    assert.ok(!error, `${c.title}: JS refused an input Python accepted: ${error?.message}`);
    assert.deepEqual({entries: result.entries.map(([strokes, text]) => [strokes, toHex(text)]), names: result.names}, c.expect, c.title);
    parsed++;
  }
}
for (const c of vectors.export) {
  const text = toPloverJson(c.entries.map(([strokes, hex]) => [strokes, fromHex(hex)]), c.names);
  assert.equal(text, c.expect, c.title);
}
console.log(`PASS Plover JSON importer (JS): ${parsed} accepted and ${refused} refused like Python, ${vectors.export.length} export files byte-identical`);
