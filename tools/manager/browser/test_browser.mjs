// Open the actual standalone HTML from disk with all networking disabled.
import assert from "node:assert/strict";
import path from "node:path";
import fs from "node:fs";
import {pathToFileURL, fileURLToPath} from "node:url";
import {createRequire} from "node:module";
const require = createRequire(import.meta.url);
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || "playwright");
const sourceDirectory = path.dirname(fileURLToPath(import.meta.url));
const file = path.resolve(process.argv[2] || "build/offline-manager/StenoSquidManager.html");
const browser = await chromium.launch({headless: true, ...(process.env.CHROME_PATH ? {executablePath: process.env.CHROME_PATH} : {})});
try {
  const context = await browser.newContext({offline: true});
  const page = await context.newPage();
  const errors = [];
  const network = [];
  page.on("pageerror", (error) => errors.push(error.message));
  page.on("request", (request) => { if (/^https?:/.test(request.url())) network.push(request.url()); });
  await page.addInitScript(() => {
    window.mock = {dictionaryReads: 0, editsReads: 0, editorBaseline: {KAT: "cat", TKOG: "dog"}, editorEdits: {KAT: "cat", TKOG: "dog"}, kind: "base", editorEntries: {KAT: "cat", TKOG: "dog"}, tail: 256, commands: [], hints: "auto", layout: "Mac U.S.", enabled: true,
      granted: ["bluetooth", "usb"], selection: "bluetooth", pickerCalls: 0, brokenUSB: false};
    function makeDevice(transport, serial, productName) {
    const device = new EventTarget();
    Object.assign(device, {
      opened: false, productName, collections: [{usagePage: 0xff31, usage: 0x74, inputReports: [{reportId: 0}], outputReports: [{reportId: 0}]}],
      async open() { this.opened = true; }, async close() { this.opened = false; }
    });
    let pending = "";
    let entry;
    device.sendReport = async (id, bytes) => {
      if (transport === "USB" && mock.brokenUSB) throw new Error("USB unplugged");
      if (id !== 0 || bytes.length !== 63) throw new Error("Invalid HID report framing");
      const text = new TextDecoder().decode(bytes).replace(/\0+$/, "");
      const prefix = text.slice(0, 4);
      pending += text.slice(4);
      if (!pending.endsWith("\n")) return;
      const command = pending.trimEnd();
      pending = "";
      mock.commands.push(command);
      let reply = "ERR Unsupported mock command";
      if (command === "native_install identity") reply = `OK SQMANAGER1 ${serial}`;
      else if (command === "native_install connection") reply = `OK ${transport}`;
      else if (command === "native_install status") reply = "NATIVE2 READY 1";
      else if (command === "user_dictionary_status") reply = JSON.stringify({format: "SQBCAT02", ready: true, read_only: false, tail: mock.tail, generation: 1});
      else if (command === "get_parameter available_host_layouts") reply = '["Mac U.S.","Windows U.S. (plain text)"]';
      else if (command === "get_parameter host_layout") reply = mock.layout;
      else if (command.startsWith("set_host_layout ")) { mock.layout = command.slice(16); reply = "OK"; }
      else if (command === "stroke_hints") reply = mock.hints;
      else if (command.startsWith("stroke_hints ")) { mock.hints = command.slice(13); reply = "OK"; }
      else if (command === "disable_dictionary user_dictionary") { mock.enabled = false; reply = "OK"; }
      else if (command === "native_entry abort") { entry = null; reply = "OK"; }
      else if (command.startsWith("native_entry begin ")) { entry = {outline: command.split(" ")[4], bytes: []}; reply = "OK"; }
      else if (command.startsWith("native_entry data ")) { entry.bytes.push(...Uint8Array.from(atob(command.split(" ")[3]), c => c.charCodeAt(0))); reply = "OK"; }
      else if (command === "native_entry commit") { mock.tail++; mock.editorEntries[entry.outline] = new TextDecoder().decode(new Uint8Array(entry.bytes)); mock.editorEdits[entry.outline] = mock.editorEntries[entry.outline]; reply = "OK"; }
      else if (command.startsWith("native_remove_entry ")) { mock.tail++; mock.editorEdits[command.split(" ")[2]] = null; delete mock.editorEntries[command.split(" ")[2]]; reply = "OK"; }
      else if (command.startsWith("native_reset_entry ")) {
        mock.tail++;
        const key = command.split(" ")[2];
        delete mock.editorEdits[key];
        if (Object.hasOwn(mock.editorBaseline, key)) mock.editorEntries[key] = mock.editorBaseline[key];
        else delete mock.editorEntries[key];
        reply = "OK";
      }
      else if (command === "native_catalog") reply = JSON.stringify([{id: 1, name: "user_dictionary", kind: mock.kind, enabled: mock.enabled, entries: 0}]);
      else if (command === "native_install update overlay-usage") reply = "ERR Unsupported mock command";
      const encoded = new TextEncoder().encode(prefix + reply + "\n\n");
      for (let at = 0; at < encoded.length; at += 63) {
        const packet = new Uint8Array(63);
        packet.set(encoded.subarray(at, at + 63));
        queueMicrotask(() => device.dispatchEvent(Object.assign(new Event("inputreport"), {reportId: 0, data: new DataView(packet.buffer)})));
      }
    };
    return device;
    }
    const devices = {
      usb: makeDevice("USB", "ABCDEF1234", "Steno Squid"),
      bluetooth: makeDevice("BLUETOOTH", "ABCDEF1234", "BLECORNE"),
      other: makeDevice("USB", "AAAA1111", "Steno Squid")
    };
    const hid = new EventTarget();
    hid.requestDevice = async () => { mock.pickerCalls++; return [devices[mock.selection]]; };
    hid.getDevices = async () => mock.granted.map(name => devices[name]);
    Object.defineProperty(navigator, "hid", {value: hid});
  
  });
  await page.goto(pathToFileURL(file).href);
  assert.equal(await page.evaluate(() => location.protocol), "file:");
  await page.evaluate(() => {
    const load = globalThis.loadPyodide;
    globalThis.loadPyodide = async (...args) => {
      const python = await load(...args);
      globalThis.testPython = python;
      return python;
    };
  });
  await page.locator("#refresh").click();
  try {
    await page.waitForFunction(() => !document.querySelector("#workspace").hidden, null, {timeout: 30000});
  } catch (error) {
    console.error(await page.evaluate(() => ({status:document.querySelector("#status").textContent, mock})));
    console.error(errors);
    throw error;
  }
  assert.match(await page.locator("#connected").textContent(), /USB/);
  await page.locator("#hints").selectOption("off");
  await page.waitForFunction(() => mock.hints === "off" && !document.querySelector("#refresh").disabled);
  await page.locator("#layout").selectOption("Windows U.S. (plain text)");
  await page.waitForFunction(() => mock.layout === "Windows U.S. (plain text)" && !document.querySelector("#refresh").disabled);
  await page.evaluate(() => testPython.runPython(`
import manager_editor, js
import struct
from types import SimpleNamespace
from native_update import Edit
from compile_dict import parse_outline

def test_editor_snapshot():
    row = bytearray(96)
    label = b"user_dictionary"
    struct.pack_into("<IIH", row, 0, 1, 1, 0)
    row[12] = len(label)
    row[16:16 + len(label)] = label
    data = json.loads(js.JSON.stringify(js.mock.editorEdits))
    entries = [Edit(0, 1, int(text is None), parse_outline(outline), b"" if text is None else text.encode()) for outline, text in data.items()]
    return SimpleNamespace(rows=[bytes(row)], entries={entry.key: entry for entry in entries})

def test_export_dictionary(*args, **kwargs):
    js.mock.dictionaryReads += 1
    if kwargs.get("on_snapshot"):
        kwargs["on_snapshot"](test_editor_snapshot())
    return json.loads(js.JSON.stringify(js.mock.editorEntries))

def test_saved_edits(*args, **kwargs):
    js.mock.editsReads += 1
    if kwargs.get("on_snapshot"):
        kwargs["on_snapshot"](test_editor_snapshot())
    return json.loads(js.JSON.stringify(js.mock.editorEdits))

manager_editor.export_dictionary = test_export_dictionary
manager_editor.saved_edits = test_saved_edits
`));
  await page.locator("#view-dictionary").click();
  await page.waitForFunction(() => document.querySelector("#editor-source").value.includes('"cat"') && !document.querySelector("#editor-preview").disabled);
  await page.locator("#editor-source").fill('{"KAT":');
  await page.locator("#editor-preview").click();
  await page.waitForFunction(() => !document.querySelector("#editor-error").hidden);
  assert.match(await page.locator("#editor-error").textContent(), /line 1/);
  assert.equal(await page.locator("#editor-source").inputValue(), '{"KAT":');
  await page.locator("#editor-source").fill('{"KAT":"kitty","HAT":"hat"}');
  await page.locator("#editor-preview").click();
  await page.waitForFunction(() => !document.querySelector("#editor-save").hidden && !document.querySelector("#editor-save").disabled).catch(async error => {
    console.error(await page.evaluate(() => ({error:document.querySelector("#editor-error").textContent,summary:document.querySelector("#editor-summary").textContent,status:document.querySelector("#status").textContent,save:document.querySelector("#editor-save").outerHTML,preview:document.querySelector("#editor-preview").outerHTML})));
    throw error;
  });
  assert.match(await page.locator("#editor-summary").textContent(), /1 added.*1 changed.*1 removed/);
  await page.screenshot({path:path.join(path.dirname(file),"dictionary-editor-test.png"),fullPage:true});
  await page.locator("#editor-save").click();
  await page.waitForFunction(() => document.querySelector("#editor-summary").textContent === "Saved 3 changes.");
  assert.deepEqual(await page.evaluate(() => mock.editorEntries), {KAT: "kitty", HAT: "hat"});
  assert.equal(await page.locator("#dictionary-editor").isVisible(), false);
  assert.equal(await page.locator("#status").textContent(), "Dictionary changes saved.");
  await page.locator("#view-dictionary").click();
  await page.waitForFunction(() => document.querySelector("#dictionary-editor").open && !document.querySelector("#editor-preview").disabled);

  await page.evaluate(() => { mock.editorEntries.TKOG = "external edit"; mock.editorEdits.TKOG = "external edit"; mock.tail++; });
  await page.locator("#editor-source").fill('{}');
  await page.locator("#editor-preview").click();
  await page.waitForFunction(() => !document.querySelector("#editor-error").hidden);
  assert.match(await page.locator("#editor-error").textContent(), /changed since/);
  assert.equal(await page.locator("#editor-source").inputValue(), '{}');
  page.once("dialog", dialog => dialog.accept());
  await page.locator("#editor-reload").click();
  await page.waitForFunction(() => document.querySelector("#editor-source").value.includes("external edit") && !document.querySelector("#editor-close").disabled);
  assert.equal(await page.getByRole("button", {name: "Download draft", exact: true}).count(), 0);
  const readsBeforeFilter = await page.evaluate(() => [mock.dictionaryReads, mock.editsReads]);
  await page.locator("#editor-filter").selectOption("edits");
  await page.waitForFunction(() => document.querySelector("#editor-count").textContent === "3 edits" && !document.querySelector("#editor-preview").disabled);
  assert.deepEqual(await page.evaluate(() => [mock.dictionaryReads, mock.editsReads]), readsBeforeFilter);
  await page.locator("#editor-source").fill('{"TKOG":"external edit","HAT":"hat"}');
  await page.locator("#editor-preview").click();
  await page.waitForFunction(() => !document.querySelector("#editor-save").hidden && !document.querySelector("#editor-save").disabled);
  assert.match(await page.locator("#editor-summary").textContent(), /1 edit undone/);
  await page.screenshot({path:path.join(path.dirname(file),"dictionary-edits-test.png"),fullPage:true});
  await page.locator("#editor-save").click();
  await page.waitForFunction(() => document.querySelector("#editor-summary").textContent === "Saved 1 change." && !document.querySelector("#editor-preview").disabled);
  assert.equal(await page.evaluate(() => mock.editorEntries.KAT), "cat");
  assert.equal(await page.evaluate(() => Object.hasOwn(mock.editorEdits, "KAT")), false);
  assert.equal(await page.locator("#dictionary-editor").isVisible(), false);
  await page.locator("#view-dictionary").click();
  await page.waitForFunction(() => document.querySelector("#dictionary-editor").open && !document.querySelector("#editor-preview").disabled);
  await page.locator("#editor-filter").selectOption("edits");
  await page.waitForFunction(() => document.querySelector("#editor-count").textContent === "2 edits" && !document.querySelector("#editor-preview").disabled);

  await page.locator("#editor-source").fill('{}');
  await page.locator("#editor-preview").click();
  await page.waitForFunction(() => !document.querySelector("#editor-save").hidden && !document.querySelector("#editor-save").disabled);
  assert.match(await page.locator("#editor-summary").textContent(), /2 edits undone/);
  await page.locator("#editor-save").click();
  await page.waitForFunction(() => document.querySelector("#editor-summary").textContent === "Saved 2 changes." && !document.querySelector("#editor-preview").disabled);
  assert.deepEqual(await page.evaluate(() => mock.editorEdits), {});
  assert.deepEqual(await page.evaluate(() => mock.editorEntries), {KAT: "cat", TKOG: "dog"});
  assert.equal(await page.locator("#dictionary-editor").isVisible(), false);
  await page.locator("#view-dictionary").click();
  await page.waitForFunction(() => document.querySelector("#dictionary-editor").open && !document.querySelector("#editor-preview").disabled);

  await page.locator("#editor-filter").selectOption("all");
  await page.waitForFunction(() => document.querySelector("#editor-source").value.includes('"cat"') && !document.querySelector("#editor-preview").disabled);
  assert.deepEqual(JSON.parse(await page.locator("#editor-source").inputValue()), {KAT: "cat", TKOG: "dog"});
  await page.locator("#editor-close").click();
  assert.equal(await page.evaluate(() => mock.pickerCalls), 0);
  // Bluetooth comes first in the enumeration; USB must still win. If USB is
  // absent or unreadable, use Bluetooth for the same confirmed keyboard.
  await page.evaluate(() => { mock.granted = ["bluetooth"]; });
  await page.locator("#refresh").click();
  await page.waitForFunction(() => document.querySelector("#connected").textContent.includes("Bluetooth") && !document.querySelector("#refresh").disabled);
  assert.equal(await page.locator('#import input[type="file"]').isDisabled(), true);
  await page.evaluate(() => { mock.granted = ["bluetooth", "usb"]; mock.brokenUSB = true; });
  await page.locator("#refresh").click();
  await page.waitForFunction(() => !document.querySelector("#refresh").disabled);
  assert.match(await page.locator("#connected").textContent(), /Bluetooth/);
  await page.evaluate(() => { mock.brokenUSB = false; });
  await page.locator("#refresh").click();
  await page.waitForFunction(() => document.querySelector("#connected").textContent.includes("USB") && !document.querySelector("#refresh").disabled);
  await page.getByRole("button", {name: "Choose device…", exact: true}).click();
  await page.waitForFunction(() => mock.pickerCalls === 1 && !document.querySelector("#refresh").disabled);
  assert.match(await page.locator("#connected").textContent(), /USB/);
  // A chooser selection establishes which physical keyboard to use; other
  // authorized keyboards must not affect its USB preference.
  await page.evaluate(() => { mock.granted = ["bluetooth", "usb", "other"]; });
  await page.getByRole("button", {name: "Choose device…", exact: true}).click();
  await page.waitForFunction(() => mock.pickerCalls === 2 && !document.querySelector("#refresh").disabled);
  assert.match(await page.locator("#connected").textContent(), /USB/);
  // An unavailable explicitly selected device produces a readable error.
  await page.evaluate(() => { mock.selection = "usb"; mock.brokenUSB = true; });
  await page.getByRole("button", {name: "Choose device…", exact: true}).click();
  await page.waitForFunction(() => mock.pickerCalls === 3 && !document.querySelector("#refresh").disabled);
  assert.match(await page.locator("#status").textContent(), /USB unplugged/);
  assert.doesNotMatch(await page.locator("#status").textContent(), /Traceback/);
  await page.evaluate(() => { mock.brokenUSB = false; mock.granted = ["usb"]; });
  await page.locator("#refresh").click();
  await page.waitForFunction(() => !document.querySelector("#workspace").hidden && !document.querySelector("#refresh").disabled);
  await page.evaluate(() => { mock.granted = []; });
  await page.locator("#refresh").click();
  await page.waitForFunction(() => mock.pickerCalls === 4 && !document.querySelector("#refresh").disabled);
  assert.match(await page.locator("#connected").textContent(), /USB/);
  // Test the production disk adapter, including refusal before a folder exists.
  const backendSource = fs.readFileSync(path.join(sourceDirectory, "backend.js"), "utf8")
    .replace('import {HidConnection} from "./hid.js";', "").replaceAll("export ", "");
  await page.addScriptTag({type: "module", content: backendSource + "\nglobalThis.diskAdapter = Object.create(BrowserBackend.prototype);"});
  await page.waitForFunction(() => Boolean(globalThis.diskAdapter));
  const disk = await page.evaluate(async () => {
    const backend = globalThis.diskAdapter;
    const path = "/home/pyodide/.stenosquid/repack/" + "a".repeat(64) + "/" + "b".repeat(32) + "/original.json";
    const absent = await backend.service({operation: "persist", path, data: "backup"});
    let committed = false;
    let saved;
    const missing = () => new DOMException("Missing", "NotFoundError");
    const handle = {
      async createWritable() { return {
        async write(data) { saved = data; },
        async close() { await new Promise(resolve => setTimeout(resolve, 20)); committed = true; },
        async abort() {}
      }; },
      async getFile() { return {text: async () => saved}; }
    };
    const plan = {async getFileHandle(name, options) { if (!committed && !options?.create) throw missing(); return handle; }};
    backend.folder = {async getDirectoryHandle() { return {async getDirectoryHandle() { return plan; }}; }};
    const write = await backend.service({operation: "persist", path, data: "backup"});
    const closedBeforeReply = committed;
    const read = await backend.service({operation: "load", path});
    const duplicate = await backend.service({operation: "persist", path, data: "replace"});
    return {absent, write, closedBeforeReply, read, duplicate};
  });
  assert.match(disk.absent.error, /Choose a recovery folder/);
  assert.equal(disk.closedBeforeReply, true);
  assert.equal(disk.read.value, "backup");
  assert.equal(disk.write.error, undefined);
  assert.match(disk.duplicate.error, /cannot be replaced/);
  // Run existing Manager regression cases in a second real Python interpreter.
  // Two host-specific cases use HTTP threads/hardlinks; the disk adapter above
  // covers the browser replacement for native file durability.
  const parity = await page.evaluate(async ({testSource, editorTests}) => {
    const python = await loadPyodide({indexURL: "https://offline.invalid/", lockFileContents: offlineAssets.lock});
    python.unpackArchive(offlineBytes(offlineAssets.runtime), "zip", {extractDir: "/app"});
    python.FS.writeFile("/app/tools/manager/test_manager.py", testSource);
    python.FS.writeFile("/app/tools/manager/test_manager_editor.py", editorTests);
    return JSON.parse(python.runPython(`
import json, sys, unittest
sys.path[:0] = ["/app/tools/manager", "/app/tools/storage", "/app/tools/dictionaries", "/app/tools/dict_compiler"]
import test_manager, test_manager_editor
excluded = {"test_local_http_routes_require_session_and_origin", "test_repack_previews_without_backup_then_requires_matching_confirmation"}
suite = unittest.TestSuite(test for test in unittest.defaultTestLoader.loadTestsFromTestCase(test_manager.ManagerTest) if test._testMethodName not in excluded)
suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(test_manager_editor.EditorTest))
result = unittest.TestResult()
suite.run(result)
json.dumps({"ran": result.testsRun, "errors": [(str(test), message) for test, message in result.errors + result.failures]})
`));
  }, {testSource: fs.readFileSync(path.resolve(sourceDirectory, "../test_manager.py"), "utf8"), editorTests: fs.readFileSync(path.resolve(sourceDirectory, "../test_manager_editor.py"), "utf8")});
  assert.equal(parity.ran, 48, JSON.stringify(parity));
  assert.deepEqual(parity.errors, [], JSON.stringify(parity));
  assert.deepEqual(errors, []);
  assert.deepEqual(network, []);
  await page.screenshot({path:path.join(path.dirname(file),"browser-test.png"),fullPage:true});
  console.log("PASS: standalone file:// app, network disabled, no HTTP requests, real Python WebAssembly, fragmented WebHID reports, USB preference and Bluetooth fallback, Manager settings, local recovery file safety, JSON editor preview/save/stale-draft handling, and 48 shared Python regression cases");
} finally { await browser.close(); }
