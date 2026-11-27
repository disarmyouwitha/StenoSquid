// node tools/manager/browser/test_repack_flow.mjs   (no browser required)
// Gate for repack_jobs.js and recover.js. Each scenario replays what manager_repack.py did against a scripted keyboard: the same console
// commands in the same order (with the same timeouts and the same staged writes), the same progress, waits, results and failures,
// and byte-identical recovery files saved to the recovery folder.
import assert from "node:assert/strict";
import fs from "node:fs";
import {createHash} from "node:crypto";
import {makeRepackJobs} from "./repack_jobs.js";

const vectors = JSON.parse(fs.readFileSync(new URL("./golden/repack_flow_vectors.json", import.meta.url), "utf8"));
const NAMES = {TimeoutError: "TimeoutError", KeyError: "KeyError", AssertionError: "AssertionError", JSONDecodeError: "JSONDecodeError",
  UnicodeDecodeError: "UnicodeDecodeError", UnicodeEncodeError: "UnicodeEncodeError", TypeError: "TypeError"};
const sha = (text) => createHash("sha256").update(text).digest("hex");
const plain = (value) => JSON.parse(JSON.stringify(value));

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
    serial: vectors.serial,
    transport: "usb",
    async command(text, timeout = 30) {
      assert.ok(next < c.transcript.length, `${c.title}: extra command ${text.slice(0, 80)}`);
      const entry = c.transcript[next++];
      assert.equal(text, entry.command, `${c.title}: command ${next - 1} differs from Python`);
      assert.equal(timeout, entry.timeout, `${c.title}: timeout of ${text.slice(0, 60)}`);
      if (entry.raise) throw Object.assign(new Error(entry.raise.message), {name: NAMES[entry.raise.type] ?? "Error", kind: entry.raise.type === "RuntimeError" ? "RuntimeError" : undefined});
      return replyOf(entry);
    },
  };
  // The recovery folder: preloaded like Python's temporary directory.
  const folder = new Map(Object.entries(c.preload));
  const key = (identity, token, name) => `${identity}/${token}/${name}.json`;
  const store = {
    async save(identity, token, name, text) {
      if (folder.has(key(identity, token, name))) throw new Error("An existing recovery file cannot be replaced.");
      folder.set(key(identity, token, name), text);
    },
    async load(identity, token, name) {
      if (!folder.has(key(identity, token, name))) throw new Error("recovery file is missing");
      return folder.get(key(identity, token, name));
    },
    async latest(identity) {
      let found = null;
      for (const name of folder.keys()) {
        const [id, token, file] = name.split("/");
        if (id === identity && file === "attempt.json" && folder.has(key(id, token, "original"))) found = token;
      }
      return found;
    },
  };
  let now = 0;
  const sleeps = [];
  const clock = {now: () => now, sleep: async (seconds) => { sleeps.push(seconds); now += seconds; }};
  const jobs = makeRepackJobs({opener: async () => ({console: console_, close: async () => {}}), clock, store, token: () => vectors.token, now: () => vectors.stamp});
  const progress = [], results = [], state = {};
  let error = null;
  try {
    for (const [name, argument] of c.ops) {
      let value = null;
      if (name === "preview") value = await jobs.previewRepack(console_, (m) => progress.push(m));
      else if (name === "capture") {
        const [source, plan] = await jobs.captureApprovedRepack(console_, argument, (m) => progress.push(m));
        Object.assign(state, {source, plan});
        value = {plan, source_sha256: source.sha256};
      } else if (name === "install") await jobs.installRepack(state.source, state.plan, (m) => progress.push(m));
      else if (name === "restore") await jobs.restoreOriginal(vectors.serial, argument, (m) => progress.push(m));
      else if (name === "capture_merge") {
        const [source, original] = await jobs.captureApprovedMerge(console_, argument, (m) => progress.push(m));
        Object.assign(state, {source, original});
        value = {source_sha256: source.sha256};
      } else if (name === "install_merge") value = await jobs.installOfflineMerge(state.source, state.original, argument, (m) => progress.push(m));
      else if (name === "last") value = await jobs.lastRepackBackup(vectors.serial);
      else assert.fail(`${c.title}: unknown operation ${name}`);
      results.push(plain(value));
    }
  } catch (e) { error = e; }
  const wanted = c.expect;
  if ("error" in wanted) {
    assert.ok(error, `${c.title}: Python raised ${wanted.error.type}: ${wanted.error.message}`);
    assert.equal(error.message, wanted.error.message, `${c.title}: error text`);
    assert.equal(error.kind === "RuntimeError", wanted.error.type === "RuntimeError", `${c.title}: RuntimeError or not (${wanted.error.type})`);
  } else {
    assert.ok(!error, `${c.title}: JS raised ${error?.message}`);
  }
  assert.deepEqual(results, wanted.results, `${c.title}: results`);
  assert.deepEqual(progress, c.progress, `${c.title}: progress`);
  assert.deepEqual(sleeps, c.sleeps, `${c.title}: waits`);
  assert.equal(next, c.transcript.length, `${c.title}: JS sent ${next} commands, Python sent ${c.transcript.length}`);
  assert.deepEqual(Object.fromEntries([...folder].map(([name, text]) => [name, sha(text)]).sort(([a], [b]) => (a < b ? -1 : 1))), c.files, `${c.title}: saved recovery files`);
}
console.log(`PASS repack and recovery flows (JS): ${vectors.cases.length} scenarios behave like manager_repack.py and native_recovery.recover_usb`);
