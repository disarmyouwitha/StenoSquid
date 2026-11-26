// node tools/manager/browser/test_layout.mjs   (no browser required)
// Gate for Manager milestone M3 (flash map): layout.js must give the same results as native_layout.py for flash maps (parse,
// paged scans, image metadata reads) and compact-image validation, send the same console commands in the same order, make
// the same flash reads, and refuse with the same message where Python wrote one. golden_layout.py supplies the vectors.
import assert from "node:assert/strict";
import crypto from "node:crypto";
import fs from "node:fs";
import {parseLayout, parseMaps, readImages, readLayout, validateCompactLayout} from "./layout.js";
import {pyJson} from "./snapshot_plan.js";

const vectors = JSON.parse(fs.readFileSync(new URL("./golden/layout_vectors.json", import.meta.url), "utf8"));
const fromHex = (hex) => Uint8Array.from(Buffer.from(hex, "hex"));

// Python's json.dumps for the shapes a flash map result has: objects keep insertion order.
function dump(value) {
  if (value && typeof value === "object" && !Array.isArray(value)) {
    return "{" + Object.entries(value).map(([key, item]) => pyJson(key) + ": " + dump(item)).join(", ") + "}";
  }
  if (Array.isArray(value)) return "[" + value.map(dump).join(", ") + "]";
  return pyJson(value);
}
const digest = (value) => crypto.createHash("sha256").update(dump(value), "utf8").digest("hex");

function checkOutcome(title, expect, actual) {
  if ("error" in expect) {
    assert.ok(actual.error, `${title}: Python refused this (${expect.error.type}: ${expect.error.message}) but JS accepted it`);
    assert.equal(actual.error.message, expect.error.message, `${title}: error text`);
    return;
  }
  assert.ok(!actual.error, `${title}: JS refused an input Python accepted: ${actual.error?.message}`);
  assert.equal(digest(actual.value), expect.value.digest, `${title}: result differs from Python (summary ${JSON.stringify(expect.value.summary).slice(0, 200)})`);
}

function sync(fn) {
  try { return {value: fn()}; } catch (error) { return {error}; }
}
async function later(fn) {
  try { return {value: await fn()}; } catch (error) { return {error}; }
}

for (const c of vectors.maps) checkOutcome(c.title, c.expect, sync(() => parseMaps(...c.args)));
for (const c of vectors.layouts) checkOutcome(c.title, c.expect, sync(() => parseLayout(c.response)));

for (const c of vectors.scans) {
  const commands = [];
  const console = {async command(text) { commands.push(text); return c.replies[commands.length - 1]; }};
  checkOutcome(c.title, c.expect, await later(() => readLayout(console)));
  assert.deepEqual(commands, c.commands, `${c.title}: console commands`);
}

for (const c of vectors.images) {
  const commands = [];
  const console = {async command(text) { commands.push(text); return c.replies[commands.length - 1]; }};
  const actual = await later(() => readImages(console));
  if ("error" in c.expect) {
    assert.ok(actual.error, `${c.title}: Python refused this (${c.expect.error.message}) but JS accepted it`);
    assert.equal(actual.error.message, c.expect.error.message, `${c.title}: error text`);
  } else {
    assert.ok(!actual.error, `${c.title}: JS refused an input Python accepted: ${actual.error?.message}`);
    assert.deepEqual(actual.value, c.expect.value, `${c.title}: images`);
  }
  assert.deepEqual(commands, c.commands, `${c.title}: console commands`);
}

for (const c of vectors.compact) {
  const image = fromHex(vectors.compactImages[c.image]);
  for (const [offset, mask] of c.flips) image[offset] ^= mask;
  const data = c.cut === null ? image : image.subarray(0, c.cut);
  const reads = [];
  const read = (address, length) => {
    reads.push([address, length]);
    const chunk = data.subarray(address - c.start, address - c.start + length);
    return c.short && c.short[0] === address ? chunk.subarray(0, chunk.length - c.short[1]) : chunk;
  };
  const actual = sync(() => validateCompactLayout(read, c.start, c.size));
  if ("error" in c.expect) {
    assert.ok(actual.error, `${c.title}: Python refused this (${c.expect.error.type}: ${c.expect.error.message}) but JS accepted it`);
    if (c.expect.error.type === "ValueError") assert.equal(actual.error.message, c.expect.error.message, `${c.title}: error text`);
  } else {
    assert.ok(!actual.error, `${c.title}: JS refused an input Python accepted: ${actual.error?.message}`);
  }
  assert.deepEqual(reads, c.reads, `${c.title}: flash reads`);
}

const total = ["maps", "layouts", "scans", "images", "compact"].reduce((sum, key) => sum + vectors[key].length, 0);
console.log(`PASS flash map and image validation (JS): ${total} cases match the Python oracle`);
