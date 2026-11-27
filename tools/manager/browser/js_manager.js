// The JavaScript Manager as the page uses it: opens the keyboard's console over WebHID, checks its identity and transport (what
// bridge.py's open_console does), and runs the requests that are fully ported (jobs.js and the operations it calls). Requests that
// still need an operation not ported yet are not claimed here (supports() is false), so the page runs them in the Python Manager.
//   createJsManager({hid, factorySource, clock}) -> {supports(action), probe(), run(request, progress, showView), connectedSerial}
// `hid` is the page's HidConnection (open(), write(report), read(timeoutMs), close()). factorySource is the editor's optional
// bundled-factory-JSON lookup; without it the editor reads a factory dictionary from the keyboard instead (same result, slower).
import {Console} from "./console.js";
import {runtimeError} from "./errors.js";
import {runJob} from "./jobs.js";
import {makeEditor} from "./editor.js";
import {exportDictionary} from "./export.js";
import {backupAll} from "./backup.js";
import {transfer} from "./publish.js";

// Requests whose whole path is ported. Everything else runs in the Python Manager until its operation is ported.
export const JS_ACTIONS = new Set(["disconnect", "refresh", "layout_map", "open_installer", "enable", "hints", "layout", "set", "remove", "reset",
  "open_editor", "preview_editor", "apply_editor", "export", "backup", "move", "rename", "import", "compact", "preview_compact"]);
const SELECTING = new Set(["refresh", "open_installer"]);

const realClock = {
  now: () => performance.now() / 1000,
  sleep: (seconds) => new Promise((resolve) => setTimeout(resolve, seconds * 1000)),
};

export function createJsManager({hid, factorySource = async () => null, clock = realClock}) {
  const device = {write: (report) => hid.write(report), read: (timeout) => hid.read(timeout)};
  const manager = {connectedSerial: null};

  // bridge.py open_console: the console must identify itself as a StenoSquid manager and say how it is connected.
  async function open(wanted = null) {
    await hid.open();
    const console = new Console(device, {clock});
    const identity = await console.command("native_install identity", 3);
    const match = /^OK SQMANAGER1 ([0-9A-F]+)$/.exec(identity);
    if (!match || (wanted && wanted !== match[1])) {
      throw runtimeError("The selected keyboard identity differs. Connect the intended Steno Squid left half.");
    }
    console.serial = match[1];
    const connection = await console.command("native_install connection", 3);
    if (connection !== "OK USB" && connection !== "OK BLUETOOTH") {
      throw runtimeError("Update the left firmware from the matching browser Manager release to identify its connection safely.");
    }
    console.transport = connection === "OK USB" ? "usb" : "bluetooth";
    return {console, close: async () => {}};  // the HID connection stays open between jobs, as in the Python bridge
  }

  const editor = makeEditor({factorySource, clock});
  const unported = (name) => async () => { throw new Error(`${name} is not available in the JavaScript Manager yet.`); };
  const deps = {
    opener: open, bluetoothOpener: open, clock,
    lastRepackBackup: async () => null,
    clearDocuments: async (serial) => editor.clearDocuments(serial),
    readDocument: (...args) => editor.readDocument(...args),
    changeDocument: (...args) => editor.changeDocument(...args),
    exportDictionary: (console, name) => exportDictionary(console, name),
    backupAll: (console) => backupAll(console),
    transfer: (console, entries, progress, options = {}) => transfer(console, entries, progress, {move: options.move ?? null,
      rename: options.rename ?? null, delete: options.delete ?? null, previewCompact: options.preview_compact === true,
      expectedCompact: options.expected_compact ?? null}),
    ...Object.fromEntries(["previewRepack", "captureApprovedRepack", "installRepack", "restoreOriginal", "captureApprovedMerge", "installOfflineMerge",
      "factoryPackage", "changeSnapshot", "clearPersonal", "mergeSnapshots", "namedImport", "prepareInstallImage", "provision"]
      .map((name) => [name, unported(name)])),
  };

  manager.supports = (action) => JS_ACTIONS.has(action);

  // Identity and transport only; used when Connect compares the authorized devices.
  manager.probe = async () => {
    try {
      const {console} = await open();
      return {serial: console.serial, transport: console.transport};
    } catch (error) {
      return {error: error.message || String(error)};
    }
  };

  // Mirrors bridge.py _run_request. A JSON object is returned for the page; failures are thrown.
  manager.run = async (request, progress, showView) => {
    if (request.action === "disconnect") {
      await hid.close();
      manager.connectedSerial = null;
      return {disconnected: true};
    }
    const expected = SELECTING.has(request.action) ? null : manager.connectedSerial;
    if (expected === null && !SELECTING.has(request.action)) throw runtimeError("Connect the keyboard before making changes.");
    const {console} = await open(expected);
    const {serial, transport} = console;
    manager.connectedSerial = serial;
    const result = await runJob(serial, request, progress, showView, {transport}, deps);
    // JSON objects keep an export's order only when they are built from entries, so the Map becomes a plain object here.
    if (result?.download instanceof Map) result.download = Object.fromEntries(result.download);
    return result;
  };
  return manager;
}
