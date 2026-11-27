// node tools/manager/browser/test_install.mjs   (no browser required)
// Gate for install.js. The image it builds must be byte-identical to build_native_image.build (same SHA-256 and report), check_image
// must judge damaged copies the same way, and provision() must send the same console commands, report the same progress and fail
// the same way as native_install.provision.
import assert from "node:assert/strict";
import fs from "node:fs";
import {createHash} from "node:crypto";
import {buildImage, checkImage, provision} from "./install.js";
import {crc32} from "./transfer_frame.js";

const vectors = JSON.parse(fs.readFileSync(new URL("./golden/install_vectors.json", import.meta.url), "utf8"));
const encoder = new TextEncoder();
const readSource = async (path) => encoder.encode(vectors.sources[path]);
const sha = (bytes) => createHash("sha256").update(bytes).digest("hex");
const plain = (value) => JSON.parse(JSON.stringify(value));
const SEGMENT = 65536;

function matches(title, error, wanted) {
  assert.ok(error, `${title}: Python raised ${wanted.message}`);
  assert.equal(error.message, wanted.message, `${title}: error text`);
  assert.equal(error.kind === "RuntimeError", wanted.type === "RuntimeError", `${title}: RuntimeError or not (${wanted.type})`);
}

// 1. Builds
const built = new Map();
for (const c of vectors.builds) {
  let result = null, error = null;
  try { result = await buildImage(readSource, c.packages, c.pool_end); } catch (e) { error = e; }
  if ("error" in c.expect) { matches(c.title, error, c.expect.error); continue; }
  assert.ok(!error, `${c.title}: JS raised ${error?.message}`);
  const [image, report] = result;
  assert.equal(image.length, c.expect.length, `${c.title}: length`);
  assert.equal(sha(image), c.expect.sha256, `${c.title}: image bytes`);
  assert.deepEqual(plain(report), c.expect.report, `${c.title}: report`);
  built.set(c.title, result);
}

// 2. check_image on damaged copies
function reseal(image) {
  const v = new DataView(image.buffer, image.byteOffset, image.byteLength);
  for (const root of [0, SEGMENT]) {
    const size = v.getUint32(root + 16, true);
    v.setUint32(root + 20, crc32(image.subarray(root + 32, root + 32 + size)), true);
    v.setUint32(root + 24, crc32(image.subarray(root, root + 24)), true);
  }
}
const sources = {all: built.get("all factory dictionaries"), plover: built.get("plover only")};
for (const c of vectors.checks) {
  const [source, sourceReport] = sources[c.source];
  const data = Uint8Array.from(source);
  for (const [at, xor] of c.edits) data[at] ^= xor;
  if (c.seal) reseal(data);
  const manifest = c.manifest === "fixed" ? {format: c.format, crc32: crc32(data), sha256: sha(data)} : {format: c.format, crc32: sourceReport.crc32, sha256: sourceReport.sha256};
  let result = null, error = null;
  try { result = await checkImage(data, manifest); } catch (e) { error = e; }
  if ("error" in c.expect) matches(c.title, error, c.expect.error);
  else {
    assert.ok(!error, `${c.title}: JS raised ${error?.message}`);
    assert.deepEqual([...result].sort((a, b) => a - b), c.expect.absent, `${c.title}: uninstalled identities`);
  }
}

// 3. provision against a scripted keyboard
for (const c of vectors.provisions) {
  const [image, report] = c.packages === "large" ? built.get("all, large pool") : await buildImage(readSource, c.packages);
  let next = 0;
  const console_ = {
    async command(text, timeout = 30) {
      const step = c.log[next++];
      assert.ok(step, `${c.title}: extra command ${text.slice(0, 60)}`);
      assert.equal(text, step.c, `${c.title}: command ${next - 1}`);
      assert.equal(timeout, step.t, `${c.title}: timeout of ${text.slice(0, 40)}`);
      return step.r;
    },
  };
  const messages = [];
  let error = null;
  try { await provision(console_, image, report, (message) => messages.push(message), {expected_status: c.expected_status}); } catch (e) { error = e; }
  if ("error" in c.expect) matches(c.title, error, c.expect.error);
  else assert.ok(!error, `${c.title}: JS raised ${error?.message}`);
  assert.deepEqual(messages, c.progress, `${c.title}: progress`);
  assert.equal(next, c.log.length, `${c.title}: JS sent ${next} of ${c.log.length} commands`);
}
console.log(`PASS install (JS): ${vectors.builds.length} builds byte-identical to Python, ${vectors.checks.length} image checks, ${vectors.provisions.length} provisioning transcripts`);
