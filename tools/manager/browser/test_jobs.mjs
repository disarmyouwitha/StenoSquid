// node tools/manager/browser/test_jobs.mjs   (no browser required)
// Gate for Manager milestone M3 (jobs): jobs.js + manage.js must do what manager.run_job does, in the same order: open and close
// the same sessions, send the same console commands with the same timeouts, call the same injected operations with the same
// arguments, report the same progress and views, wait the same sleeps, and finish with the same result or failure. One ordered
// event stream per case comes from golden_jobs.py; any difference in order or content fails with the first divergence.
import assert from "node:assert/strict";
import fs from "node:fs";
import {runJob} from "./jobs.js";
import {importEntries} from "./manage.js";
import {parseNamed} from "./importer.js";
import {runtimeError} from "./errors.js";

const vectors = JSON.parse(fs.readFileSync(new URL("./golden/jobs_vectors.json", import.meta.url), "utf8"));
const roundTrip = (value) => JSON.parse(JSON.stringify(value));
const hex = (bytes) => Buffer.from(bytes).toString("hex");

// Python's exception types as the ported code sees them.
function makeError({type, message}) {
  if (type === "RuntimeError") return runtimeError(message);
  if (type === "ValueError" || type === "OSError") return new Error(message);
  return Object.assign(new Error(message), {name: type});
}

function revive(value) {
  if (Array.isArray(value)) return value.map(revive);
  if (value && typeof value === "object") {
    const keys = Object.keys(value);
    if (keys.length === 1 && keys[0] === "bytes") return Uint8Array.from(Buffer.from(value.bytes, "hex"));
    return Object.fromEntries(keys.map((key) => [key, revive(value[key])]));
  }
  return value;
}

const entriesOf = (entries) => (entries === null ? null : entries.map(([strokes, text]) => [strokes, hex(text)]));

const DESCRIBE = {
  clear_documents: (serial) => ({serial}),
  read_document: (console, selected, progress, o) => ({selected, force: o.force, mode: o.mode}),
  change_document: (console, selected, request, progress, o) => ({selected, request, apply: o.apply}),
  preview_repack: () => ({}),
  capture_approved_repack: (console, request) => ({request}),
  install_repack: (source, plan) => ({source, plan}),
  restore_original: (serial, request) => ({serial, request}),
  capture_approved_merge: (console, request) => ({request}),
  install_offline_merge: (source, original, request) => ({source, original, request}),
  factory_package: (console, pkg, o) => ({package: pkg, restore: o.restore, confirmation: o.confirmation}),
  change_snapshot: (console, operation, name, o) => ({operation, name, stage: o.stage ?? false, preview: o.preview ?? false, expected: o.expected ?? null, target_id: o.target_id ?? null}),
  clear_personal: (console, id) => ({id}),
  merge_snapshots: (console, upper, lower, o) => ({upper, lower, expected: o.expected}),
  named_import: (console, name, entries, o) => ({name, entries: entriesOf(entries), stage: o.stage, expected: o.expected}),
  transfer: (console, entries, progress, options) => ({entries: entriesOf(entries), options}),
  export_dictionary: (console, name) => ({name}),
  backup_all: () => ({}),
  prepare_install_image: (packages) => ({packages}),
  provision: (console, image, manifest, progress, o) => ({image: hex(image), manifest, expected_status: o.expected_status}),
};
const CAMEL = {
  clear_documents: "clearDocuments", read_document: "readDocument", change_document: "changeDocument", preview_repack: "previewRepack",
  capture_approved_repack: "captureApprovedRepack", install_repack: "installRepack", restore_original: "restoreOriginal",
  capture_approved_merge: "captureApprovedMerge", install_offline_merge: "installOfflineMerge", factory_package: "factoryPackage",
  change_snapshot: "changeSnapshot", clear_personal: "clearPersonal", merge_snapshots: "mergeSnapshots", named_import: "namedImport",
  transfer: "transfer", export_dictionary: "exportDictionary", backup_all: "backupAll", prepare_install_image: "prepareInstallImage",
  provision: "provision",
};

function replyOf(event) {
  if (event.r && typeof event.r === "object") throw makeError(event.r.raise);
  return event.r;
}

async function replay(c, run) {
  let next = 0;
  const clock = {value: 1000.0};
  const take = (kind) => {
    const event = c.events[next++];
    assert.ok(event, `${c.title}: JS did more than Python (extra ${kind})`);
    assert.equal(event.e, kind, `${c.title}: step ${next - 1}: Python did '${event.e}' but JS did '${kind}'`);
    return event;
  };
  const consoleFor = () => ({
    serial: c.worldSerial ?? "AB12",
    transport: c.transport,
    async command(text, timeout = 30) {
      const event = take("cmd");
      assert.equal(text, event.c, `${c.title}: step ${next - 1}: command`);
      assert.equal(timeout, event.t, `${c.title}: timeout of ${text}`);
      clock.value += event.dt;
      return replyOf(event);
    },
  });
  const opener = (via) => async (serial) => {
    const event = take("open");
    assert.equal(via, event.via, `${c.title}: step ${next - 1}: which opener`);
    assert.equal(serial ?? null, event.serial, `${c.title}: serial asked of the opener`);
    if (event.raise) throw makeError(event.raise);
    return {console: consoleFor(), close: async () => { take("close"); }};
  };
  const deps = {
    opener: opener("usb"),
    bluetoothOpener: opener("bluetooth"),
    clock: {now: () => clock.value, async sleep(seconds) { const event = take("sleep"); assert.equal(seconds, event.s, `${c.title}: sleep`); clock.value += seconds; }},
    lastRepackBackup: async () => null,
  };
  for (const [python, camel] of Object.entries(CAMEL)) {
    deps[camel] = async (...args) => {
      const event = take("call");
      assert.equal(python, event.n, `${c.title}: step ${next - 1}: which operation`);
      assert.deepEqual(roundTrip(DESCRIBE[python](...args)), event.a, `${c.title}: arguments of ${python}`);
      if (event.raise) throw makeError(event.raise);
      return revive(event.ret);
    };
  }
  const progress = (message) => { const event = take("progress"); assert.equal(message, event.m, `${c.title}: progress message`); };
  const showView = (view) => { const event = take("view"); assert.deepEqual(roundTrip(view), event.v, `${c.title}: view shown`); };

  let actual;
  try {
    actual = {value: roundTrip(await run({progress, showView, deps, consoleFor}))};
  } catch (error) {
    if (error.name === "AssertionError" && error.code === "ERR_ASSERTION") throw error;
    actual = {error};
  }
  const expected = c.expect;
  if ("error" in expected) {
    assert.ok(actual.error, `${c.title}: Python raised ${expected.error.type} (${expected.error.message}) but JS returned a result`);
    const {type, message} = expected.error;
    if (type === "RuntimeError") {
      assert.equal(actual.error.kind, "RuntimeError", `${c.title}: should be a RuntimeError, got ${actual.error.name}: ${actual.error.message}`);
      if (message.endsWith("<json error>")) {
        // Python wraps its own JSON decoder text here; JS has a different decoder, so only the prefix is a contract.
        assert.ok(actual.error.message.startsWith(message.slice(0, -"<json error>".length)), `${c.title}: error text (${actual.error.message})`);
      } else {
        assert.equal(actual.error.message, message, `${c.title}: error text`);
      }
    } else if (type === "ValueError") {
      assert.ok(actual.error.kind !== "RuntimeError" && actual.error.name === "Error", `${c.title}: should be a ValueError, got ${actual.error.name}: ${actual.error.message}`);
      assert.equal(actual.error.message, message, `${c.title}: error text`);
    } else {
      assert.equal(actual.error.name, type, `${c.title}: kind of failure (${actual.error.message})`);
      if (["TimeoutError", "KeyError", "AssertionError"].includes(type)) assert.equal(actual.error.message, message, `${c.title}: error text`);
    }
  } else {
    assert.ok(!actual.error, `${c.title}: JS raised ${actual.error?.name}: ${actual.error?.message} but Python returned a result`);
    assert.deepEqual(actual.value, expected.value, `${c.title}: result`);
  }
  assert.equal(next, c.events.length, `${c.title}: JS stopped after ${next} of ${c.events.length} steps; Python's next was '${c.events[next]?.e}'`);
}

for (const c of vectors.cases) {
  await replay(c, ({progress, showView, deps}) => runJob(c.serial, structuredClone(c.request), progress, showView, {transport: c.transport}, deps));
}
for (const c of vectors.direct) {
  await replay({...c, serial: null, transport: "usb", request: {}}, async ({progress, deps, consoleFor}) => {
    const {entries, names} = parseNamed(c.source);
    await importEntries(consoleFor(), c.identity, entries, names, progress, deps.clock);
    return null;
  });
}
console.log(`PASS job orchestration (JS): ${vectors.cases.length} jobs and ${vectors.direct.length} direct entry imports behave like manager.py`);
