// node tools/manager/browser/test_compile.mjs   (no browser required)
// Gate for Manager milestones M1/M2 (compiler): compile.js must produce byte-identical JSC4 images, pointer offsets and
// translation-length warnings to Python's DictionaryCollectionBuilder, and compileSnapshot must accept and refuse exactly
// what compile_snapshot does, with identical output.
import assert from "node:assert/strict";
import {createHash} from "node:crypto";
import fs from "node:fs";
import {compile, compileSnapshot} from "./compile.js";

const vectors = JSON.parse(fs.readFileSync(new URL("./golden/compile_vectors.json", import.meta.url), "utf8"));
const sha = (bytes) => createHash("sha256").update(bytes).digest("hex");
const fromHex = (hex) => Uint8Array.from(Buffer.from(hex, "hex"));
const pyList = (list) => "[" + list.join(", ") + "]";

let compiled = 0, snapshots = 0, refused = 0;
for (const c of vectors.cases) {
  let actual, error = null;
  try {
    if (c.kind === "compile") {
      const {image, pointerOffsets, warnings} = compile(c.dictionaries, {baseAddr: c.baseAddr, maxTranslationBytes: c.maxTranslationBytes});
      actual = {length: image.length, sha256: sha(image), pointers: sha(pyList(pointerOffsets)), pointerCount: pointerOffsets.length, warnings: warnings.map((w) => [w.bytes])};
    } else {
      const result = compileSnapshot(c.name, c.entries.map(([strokes, hex]) => [strokes, fromHex(hex)]));
      actual = {length: result.data.length, sha256: sha(result.data), pointers: sha(pyList(result.pointers)), pointerCount: result.pointers.length};
    }
  } catch (e) { error = e; }
  if ("error" in c.expect) {
    assert.ok(error, `${c.title}: Python refused this input (${c.expect.error}) but JS accepted it`);
    if (c.kind === "compile") assert.equal(error.message, c.expect.error, `${c.title}: error text`);
    else if (c.expect.error === "ValueError") assert.equal(error.message, c.expect.message, `${c.title}: error text`);
    refused++;
  } else {
    assert.ok(!error, `${c.title}: JS refused an input Python accepted: ${error?.message}`);
    assert.deepEqual(actual, c.expect, c.title);
    if (c.kind === "compile") compiled++; else snapshots++;
  }
}
console.log(`PASS dictionary compiler (JS): ${compiled} images and ${snapshots} snapshots byte-identical to Python, ${refused} refusals matched`);
