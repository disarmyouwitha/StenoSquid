// node tools/manager/browser/test_repack.mjs   (no browser required)
// Gate for repack.js: building a repacked or merged recovery bundle must give the same image (SHA-256), the same signed bundle and
// the same report as native_repack, validate_pair must refuse the same damaged bundles, and relocate_compact / verify_hints must
// accept and refuse the same compact blobs.
import assert from "node:assert/strict";
import fs from "node:fs";
import {createHash} from "node:crypto";
import {buildRepack, buildMergeRepack, validatePair, relocateCompact, verifyHints} from "./repack.js";
import {reconstruct, digest, dumps} from "./recovery.js";

const vectors = JSON.parse(fs.readFileSync(new URL("./golden/repack_vectors.json", import.meta.url), "utf8"));
const sha = (bytes) => createHash("sha256").update(bytes).digest("hex");
const plain = (value) => JSON.parse(JSON.stringify(value));
const fromHex = (text) => Uint8Array.from(Buffer.from(text, "hex"));

function matches(title, error, wanted) {
  assert.ok(error, `${title}: Python raised ${wanted.type}: ${wanted.message}`);
  assert.equal(error.message, wanted.message, `${title}: error text`);
}

// The same changes golden_repack.py makes to an optimized bundle (keyed by their titles).
const TWEAKS = {
  "an unmodified optimized bundle": () => {},
  "a different source bundle hash": (b) => { b.repack.source_bundle_sha256 = "0".repeat(64); },
  "a different target image hash": (b) => { b.repack.target_image_sha256 = "0".repeat(64); },
  "another host layout": (b) => { b.host_layout = "Windows U.S. (plain text)"; },
  "no repack section": (b) => { delete b.repack; },
  "conversion list is not a list": (b) => { b.repack.converted = "Custom A"; },
  "conversion list has a duplicate": (b) => { b.repack.converted = ["Custom A", "Custom A"]; },
  "conversion list has a number": (b) => { b.repack.converted = [1]; },
  "conversion of a factory dictionary": (b) => { b.repack.converted = ["Lapwing Base"]; },
  "conversion list claims fewer": (b) => { b.repack.converted = []; },
  "conversion list claims more": (b) => { b.repack.converted = ["user_dictionary", "Custom A"]; },
  "merge list with a conversion": (b) => { b.repack.merged = [8, 9]; },
  "a merge pair of the wrong length": (b) => { b.repack.converted = []; b.repack.merged = [8]; },
  "a merge pair with a non-number": (b) => { b.repack.converted = []; b.repack.merged = ["a", 9]; },
};

const built = new Map();   // "bundle|names" -> [image, bundle, report] built by JS
let counts = {build: 0, merge: 0, pair: 0, relocate: 0, hints: 0};
for (const c of vectors.cases) {
  if (c.kind === "blob") continue;
  let result = null, error = null;
  if (c.kind === "build") {
    try { result = await buildRepack(vectors.bundles[c.bundle], c.names); } catch (e) { error = e; }
    if ("error" in c.expect) matches(c.title, error, c.expect.error);
    else {
      assert.ok(!error, `${c.title}: JS raised ${error?.message}`);
      const [image, bundle, report] = result;
      const want = c.expect.ok;
      assert.equal(sha(image), want.image_sha256, `${c.title}: packed image`);
      assert.equal(image.length, want.image_length, `${c.title}: image length`);
      assert.equal(bundle.sha256, want.bundle_sha256, `${c.title}: bundle digest`);
      assert.deepEqual(plain(bundle.repack), want.repack, `${c.title}: repack section`);
      assert.equal(bundle.extents.length, want.extents, `${c.title}: extents`);
      assert.deepEqual(plain(report), want.report, `${c.title}: report`);
      built.set(`${c.bundle}|${JSON.stringify(c.names)}`, result);
    }
    counts.build++;
  } else if (c.kind === "merge") {
    try {
      const bundle = vectors.bundles[c.bundle];
      result = await buildMergeRepack(bundle, await reconstruct(bundle), c.upper, c.lower);
    } catch (e) { error = e; }
    if ("error" in c.expect) matches(c.title, error, c.expect.error);
    else {
      assert.ok(!error, `${c.title}: JS raised ${error?.message}`);
      const [bundle, name] = result, want = c.expect.ok;
      assert.equal(bundle.sha256, want.bundle_sha256, `${c.title}: bundle digest`);
      assert.deepEqual(plain(bundle.repack), want.repack, `${c.title}: repack section`);
      assert.equal(name, want.name, `${c.title}: merged name`);
      assert.equal(bundle.extents.length, want.extents, `${c.title}: extents`);
    }
    counts.merge++;
  } else if (c.kind === "pair") {
    const source = vectors.bundles[c.bundle];
    const base = built.get(`${c.bundle}|${JSON.stringify(c.names)}`);
    assert.ok(base, `${c.title}: the bundle to damage was not built`);
    const bundle = structuredClone(base[1]);
    TWEAKS[c.changes](bundle);
    bundle.sha256 = await digest(bundle);
    try {
      const report = await validatePair(source, bundle, await reconstruct(source), await reconstruct(bundle));
      result = sha(dumps(plain(report)));
    } catch (e) { error = e; }
    if ("error" in c.expect) matches(c.title, error, c.expect.error);
    else {
      assert.ok(!error, `${c.title}: JS raised ${error?.message}`);
      assert.equal(result, c.expect.ok, `${c.title}: report digest`);
    }
    counts.pair++;
  } else if (c.kind === "relocate") {
    const pointers = [];
    try { result = relocateCompact(fromHex(c.hex), c.old_base, c.new_base, pointers); } catch (e) { error = e; }
    if ("error" in c.expect) matches(c.title, error, c.expect.error);
    else {
      assert.ok(!error, `${c.title}: JS raised ${error?.message}`);
      assert.equal(sha(result), c.expect.ok.sha256, `${c.title}: relocated bytes`);
      assert.deepEqual(pointers, c.expect.ok.pointers, `${c.title}: pointer fields`);
    }
    counts.relocate++;
  } else if (c.kind === "hints") {
    try { verifyHints(fromHex(c.hex), c.base); } catch (e) { error = e; }
    if ("error" in c.expect) matches(c.title, error, c.expect.error);
    else assert.ok(!error, `${c.title}: JS raised ${error?.message}`);
    counts.hints++;
  } else assert.fail(`${c.title}: unknown case kind`);
}
console.log(`PASS repack (JS): ${counts.build} builds and ${counts.merge} merges identical to native_repack, ${counts.pair} bundle checks, ${counts.relocate} relocations, ${counts.hints} hint checks`);
