// node tools/manager/browser/test_page_bundle.mjs <bundle.js>   (the Python test builds the bundle with tools/ci/bundle_modules.py)
// Smoke test of the page's JavaScript path as the offline HTML will run it: the bundled modules are evaluated in a bare context,
// then js_manager talks to a fake keyboard through the same 63-byte console reports as WebHID. A recorded Python transcript of a
// refresh (golden/state_vectors.json) supplies the keyboard's replies, so this also checks that the page-level wiring (identity and
// transport check, job routing, view/progress callbacks, result shape) reaches the same answer as the Python Manager.
import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";
import {webcrypto} from "node:crypto";

const bundle = fs.readFileSync(process.argv[2], "utf8");
const context = {console, performance, TextEncoder, TextDecoder, crypto: webcrypto, atob, btoa, setTimeout, clearTimeout, structuredClone,
  addEventListener() {}, document: {}};
vm.createContext(context);
vm.runInContext(bundle + "\nglobalThis.__modules = __modules;", context);
const {createJsManager, JS_ACTIONS} = context.__modules.js_manager;
assert.ok(context.__modules.backend.BrowserBackend, "the bundle exports BrowserBackend for the page");

const vectors = JSON.parse(fs.readFileSync(new URL("./golden/state_vectors.json", import.meta.url), "utf8"));
const wanted = vectors.cases.find((c) => c.fn === "loadView" && c.action === "refresh" && c.expect.value && !c.sleeps.length && !c.transcript.some((t) => t.raise));
assert.ok(wanted, "a recorded refresh transcript");

function replyOf(entry) {
  if (entry.reply && typeof entry.reply === "object") {
    const data = new Uint8Array(entry.reply.count).fill(0xff);
    data.set(Buffer.from(entry.reply.read, "hex"));
    return "OK " + Buffer.from(data).toString("base64");
  }
  return entry.reply;
}

// A keyboard that checks every command against the script and answers in console reports.
function fakeHid(script, label) {
  const encoder = new TextEncoder(), decoder = new TextDecoder();
  const state = {line: [], prefix: null, queue: [], opened: 0, closed: 0, script: [...script]};
  return {
    state,
    async open() { state.opened++; },
    async close() { state.closed++; },
    async write(report) {
      assert.equal(report.length, 63, "reports are 63 bytes");
      let end = report.length;
      while (end > 0 && report[end - 1] === 0) end--;
      state.prefix = report.slice(0, 4);
      state.line.push(...report.slice(4, end));
      if (state.line.at(-1) !== 10) return;
      const text = decoder.decode(Uint8Array.from(state.line.slice(0, -1)));
      state.line = [];
      const step = state.script.shift();
      assert.ok(step, `${label}: unexpected extra command ${text}`);
      assert.equal(text, step[0], `${label}: command`);
      const bytes = [...state.prefix, ...encoder.encode(step[1] + "\n\n")];
      for (let offset = 0; offset < bytes.length; offset += 63) {
        const packet = bytes.slice(offset, offset + 63);
        while (packet.length < 63) packet.push(0);
        state.queue.push(packet);
      }
    },
    async read() { return state.queue.shift() ?? []; },
  };
}
// Objects built inside the vm context have another realm's prototypes; compare their JSON.
const plain = (value) => JSON.parse(JSON.stringify(value));
const identity = [["native_install identity", "OK SQMANAGER1 AB12"], ["native_install connection", "OK USB"]];

// 1. Which requests the page runs in JavaScript.
for (const action of ["refresh", "layout_map", "open_installer", "enable", "hints", "layout", "set", "remove", "reset", "open_editor", "preview_editor",
  "apply_editor", "export", "backup", "disconnect", "move", "rename", "delete", "import", "compact", "preview_compact", "preview_import", "apply_import",
  "preview_optimize", "apply_optimize", "preview_merge", "apply_merge", "clear_personal", "preview_package", "apply_package", "setup"]) assert.ok(JS_ACTIONS.has(action), action);
for (const action of ["apply_repack", "preview_repack", "restore_repack_backup", "apply_offline_merge"]) {
  assert.ok(!JS_ACTIONS.has(action), `${action} must stay in the Python Manager until it is ported`);
}

// 2. Probing a connection reads identity and transport and nothing else.
{
  const hid = fakeHid(identity, "probe");
  const manager = createJsManager({hid});
  assert.deepEqual(plain(await manager.probe()), {serial: "AB12", transport: "usb"});
  assert.equal(hid.state.script.length, 0);
}
for (const [reply, message] of [["OK SQMANAGER1 ab12", /identity differs/], ["ERR nope", /identity differs/], ["OK SQMANAGER1 AB12 extra", /identity differs/]]) {
  const manager = createJsManager({hid: fakeHid([["native_install identity", reply]], "bad identity")});
  assert.match((await manager.probe()).error, message);
}
{
  const manager = createJsManager({hid: fakeHid([identity[0], ["native_install connection", "ERR unknown"]], "old firmware")});
  assert.match((await manager.probe()).error, /Update the left firmware/);
}

// 3. A refresh through the real console framing reaches the Python Manager's answer.
{
  const replies = wanted.transcript.map((entry) => [entry.command, replyOf(entry)]);
  const hid = fakeHid([...identity, ...identity, ...replies], "refresh");
  const manager = createJsManager({hid});
  const shown = [], progress = [];
  const result = await manager.run({action: "refresh"}, (message) => progress.push(message), (view) => shown.push(JSON.parse(JSON.stringify(view))));
  assert.deepEqual(plain(result), wanted.expect.value, "the refreshed view");
  assert.deepEqual(plain(shown), wanted.shown, "views shown while loading");
  assert.deepEqual(plain(progress), wanted.progress, "progress messages");
  assert.equal(hid.state.script.length, 0, "every recorded command was sent");
  assert.equal(manager.connectedSerial, "AB12");
}

// 4. Changing things needs a connected keyboard; another keyboard is refused; disconnect closes the connection.
{
  const manager = createJsManager({hid: fakeHid([], "unconnected")});
  await assert.rejects(() => manager.run({action: "enable", id: 1}, () => {}, () => {}), /Connect the keyboard before making changes/);
}
{
  const hid = fakeHid([["native_install identity", "OK SQMANAGER1 CD34"]], "other keyboard");
  const manager = createJsManager({hid});
  manager.connectedSerial = "AB12";
  await assert.rejects(() => manager.run({action: "enable", id: 1}, () => {}, () => {}), /identity differs/);
}
{
  const hid = fakeHid([], "disconnect");
  const manager = createJsManager({hid});
  manager.connectedSerial = "AB12";
  assert.deepEqual(plain(await manager.run({action: "disconnect"}, () => {}, () => {})), {disconnected: true});
  assert.equal(hid.state.closed, 1);
  assert.equal(manager.connectedSerial, null);
}
// 5. A request that is not ported fails loudly if it is ever routed here.
{
  const hid = fakeHid([...identity, ...identity, ["native_install status", "NATIVE2 READY 12"]], "unported");
  const manager = createJsManager({hid});
  manager.connectedSerial = "AB12";
  await assert.rejects(() => manager.run({action: "preview_repack"}, () => {}, () => {}), /./);
}
console.log("PASS page bundle (JS): the bundled modules load, route requests and refresh through the console like the Python Manager");
