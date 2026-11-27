// node tools/manager/browser/test_recovery.mjs   (no browser required)
// Gate for the recovery bundle: recovery.js must accept and reject exactly what native_recovery.py does (a valid backup, ~1,900
// damaged variants re-signed so each deeper check is reached) with the same failure, and must capture the same bundle byte for
// byte from the same reads. Python is the oracle; golden_recovery.py wrote the vectors.
import assert from "node:assert/strict";
import fs from "node:fs";
import {Snapshot, SMALL_POOL_END} from "./snapshot.js";
import {crc32} from "./transfer_frame.js";
import {capture, digest, reconstruct, unpack, pack} from "./recovery.js";

const vectors = JSON.parse(fs.readFileSync(new URL("./golden/recovery_vectors.json", import.meta.url), "utf8"));
const base = vectors.base;

const SPECIAL = {
  huge: () => "A".repeat(Math.floor((1023 * 65536 + 2) / 3) * 4 + 4),
  catalog_cut: (b) => b.catalog.slice(0, -4), catalog_more: (b) => b.catalog + "AAAA",
  extents_less: (b) => b.extents.slice(0, -1), extents_more: (b) => [...b.extents, "AAAA"],
  extent0_cut: (b) => b.extents[0].slice(0, -4), extent0_bad: (b) => "!!!!" + b.extents[0].slice(4), extent6_cut: (b) => b.extents[6].slice(0, -4),
};
const resolve = (value) => (value && typeof value === "object" && !Array.isArray(value) && Object.keys(value).length === 1 && "$" in value ? SPECIAL[value.$](base) : value);

async function apply(source, ops) {
  const bundle = structuredClone(source);
  let resign = true;
  for (const op of ops) {
    if (op[0] === "set" || op[0] === "del") {
      let target = bundle;
      for (const key of op[1].slice(0, -1)) target = target[key];
      const last = op[1].at(-1);
      if (op[0] === "set") target[last] = resolve(op[2]); else delete target[last];
    } else if (op[0] === "catalog") {
      const payload = unpack(bundle.catalog);
      payload[op[1]] = op[2];
      bundle.catalog = pack(payload);
    } else if (op[0] === "extent") {
      const [, index, offset, value, fix] = op;
      const data = unpack(bundle.extents[index]);
      data[offset] = value;
      bundle.extents[index] = pack(data);
      if (fix) {
        const payload = unpack(bundle.catalog);
        const view = new DataView(payload.buffer);
        view.setUint32(32 + view.getUint16(4, true) * 96 + index * 24 + 8, crc32(data), true);
        bundle.catalog = pack(payload);
      }
    } else if (op[0] === "nosign") resign = false;
    else throw new Error(op[0]);
  }
  if (resign && bundle !== null && typeof bundle === "object" && !Array.isArray(bundle)) bundle.sha256 = await digest(bundle);
  return bundle;
}

// Python's exception classes as the JS port raises them; the message must match only where it is the port's own text.
function sameFailure(label, expected, error) {
  assert.ok(error, `${label}: Python raised ${expected.type} (${expected.message}) but JS accepted it`);
  const own = !["UnicodeDecodeError", "UnicodeEncodeError", "Error", "IndexError", "error"].includes(expected.type);
  if (expected.type === "AssertionError") assert.equal(error.name, "AssertionError", `${label}: kind`);
  if (own) assert.equal(error.message, expected.message, `${label}: error text`);
}

let ok = 0, rejected = 0;
for (const c of vectors.cases) {
  const bundle = "bundle" in c ? c.bundle : await apply(base, c.ops);
  let result = null, error = null;
  try { result = await reconstruct(bundle); } catch (e) { error = e; }
  if ("error" in c.expect) {
    sameFailure(c.title, c.expect.error, error);
    rejected++;
  } else {
    assert.ok(!error, `${c.title}: JS rejected a backup Python accepts: ${error?.message}`);
    const {snapshot, image} = result;
    assert.deepEqual({rows: snapshot.rows.length, entries: snapshot.entries.size, extents: snapshot.extents.length, pool_end: snapshot.poolEnd, image_crc: crc32(image)}, c.expect.ok, `${c.title}: what was reconstructed`);
    ok++;
  }
}

// capture: the reads it asks for, in order, and the bundle (or failure) it produces
const payload = unpack(base.catalog);
for (const c of vectors.captures) {
  const tweaks = new Map(c.tweaks);
  const source = [];  // the flash bytes Python read for each extent (trailing erased bytes were trimmed in the vector)
  let index = 0;
  const read = (start, size) => {
    const step = c.reads[index], tweak = tweaks.get(index);
    index++;
    if (tweak?.raise) throw new Error(tweak.raise);
    assert.ok(step, `${c.title}: JS read more than Python (${start}, ${size})`);
    assert.equal(start, step.start, `${c.title}: read ${index - 1} address`);
    assert.equal(size, step.size, `${c.title}: read ${index - 1} size`);
    const data = new Uint8Array(step.full).fill(0xff);
    data.set(Buffer.from(step.hex, "hex"));
    return data;
  };
  const snapshot = await Snapshot.load((address, size) => payload.slice(address - 32, address - 32 + size), 32, payload.length, crc32(payload), {metadataOnly: true, poolEnd: SMALL_POOL_END});
  let result = null, error = null;
  try { result = await capture(read, snapshot, c.layout); } catch (e) { error = e; }
  if ("error" in c.expect) {
    if (tweaks.size && [...tweaks.values()].some((t) => t.raise)) assert.equal(error?.message, c.expect.error.message, `${c.title}: error`);
    else sameFailure(c.title, c.expect.error, error);
  } else {
    assert.ok(!error, `${c.title}: JS failed a capture Python completes: ${error?.message}`);
    assert.deepEqual(result, c.expect.bundle, `${c.title}: the bundle`);
  }
  const wanted = c.reads.length + ([...tweaks.values()].some((t) => t.raise) ? 1 : 0);  // the failing read is attempted but not logged
  assert.equal(index, wanted, `${c.title}: JS made ${index} reads, Python made ${wanted}`);
}
console.log(`PASS recovery bundles (JS): ${ok} accepted and ${rejected} rejected backups and ${vectors.captures.length} captures behave like native_recovery.py`);
