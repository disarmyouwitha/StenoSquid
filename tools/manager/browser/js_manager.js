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
import {buildImage, provision} from "./install.js";
import {makeRepackJobs} from "./repack_jobs.js";
import {mergeSnapshots, clearPersonal, changeSnapshot, namedImport, factoryPackage} from "./snapshot_ops.js";

// Requests whose whole path is ported. Everything else runs in the Python Manager until its operation is ported.
export const JS_ACTIONS = new Set(["disconnect", "refresh", "layout_map", "open_installer", "enable", "hints", "layout", "set", "remove", "reset",
  "open_editor", "preview_editor", "apply_editor", "export", "backup", "move", "rename", "delete", "import", "compact", "preview_compact", "preview_import", "apply_import", "preview_optimize", "apply_optimize",
  "apply_merge", "preview_merge", "clear_personal", "preview_package", "apply_package", "setup", "preview_repack", "apply_repack", "restore_repack_backup", "apply_offline_merge"]);
const SELECTING = new Set(["refresh", "open_installer"]);

const realClock = {
  now: () => performance.now() / 1000,
  sleep: (seconds) => new Promise((resolve) => setTimeout(resolve, seconds * 1000)),
};

export function createJsManager({hid, factorySource = async () => null, prepare = undefined, readSource = undefined, recoveryStore = null, clock = realClock}) {
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
  const repack = makeRepackJobs({opener: open, clock, store: recoveryStore});
  const deps = {
    opener: open, bluetoothOpener: open, clock,
    clearDocuments: async (serial) => editor.clearDocuments(serial),
    readDocument: (...args) => editor.readDocument(...args),
    changeDocument: (...args) => editor.changeDocument(...args),
    exportDictionary: (console, name) => exportDictionary(console, name),
    backupAll: (console) => backupAll(console),
    changeSnapshot: (console, operation, name, options) => changeSnapshot(console, operation, name, options),
    clearPersonal: (console, identity, progress) => clearPersonal(console, identity, progress),
    mergeSnapshots: (console, upper, lower, options) => mergeSnapshots(console, upper, lower, options),
    namedImport: (console, name, entries, options) => namedImport(console, name, entries, options),
    factoryPackage: (console, pkg, options) => factoryPackage(console, pkg, options, prepare),
    transfer: (console, entries, progress, options = {}) => transfer(console, entries, progress, {move: options.move ?? null,
      rename: options.rename ?? null, delete: options.delete ?? null, previewCompact: options.preview_compact === true,
      expectedCompact: options.expected_compact ?? null}),
    prepareInstallImage: async (packages) => {
      if (!readSource) throw new Error("Factory dictionaries are not available in the JavaScript Manager yet.");
      return buildImage(readSource, packages);
    },
    provision: (console, image, manifest, progress, options) => provision(console, image, manifest, progress, options),
    ...repack,
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
