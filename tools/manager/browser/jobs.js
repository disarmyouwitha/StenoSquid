// Manager milestone M3: the job orchestration, ported from tools/manager/manager.py (run_job, setup_keyboard, safe_name,
// read_import) and wait_ready in tools/storage/native_install.py. Every request check, message and ordering is the Python one.
// The heavy operations are injected through `deps` so each can be ported and gated on its own:
//   opener(serial) / bluetoothOpener(serial) -> Promise<{console, close()}>   (identity and transport already verified)
//   clock {now(), sleep(seconds)}, lastRepackBackup(serial), clearDocuments(serial)
//   readDocument, changeDocument, previewRepack, captureApprovedRepack, installRepack, restoreOriginal,
//   captureApprovedMerge, installOfflineMerge, factoryPackage, changeSnapshot, clearPersonal, mergeSnapshots, namedImport,
//   transfer, exportDictionary, backupAll, prepareInstallImage, provision
// golden_jobs.py records every one of these calls in order with the console traffic.
import {isRuntimeError, runtimeError} from "./errors.js";
import {parseOutline} from "./lookup.js";
import {parseNamed, pyRepr} from "./importer.js";
import {pyJson} from "./snapshot_plan.js";
import {readLayout} from "./layout.js";
import {checked, importEntries} from "./manage.js";
import {
  FACTORY_CAPABILITIES, FACTORY_CHOICES, UNPROVISIONED, imageDetails, installerView, isObject, loadView, mergeLimit, need, parseJson,
  readCatalog, reportable, truthy,
} from "./state.js";

const REPACK_ACTIONS = new Set(["preview_repack", "apply_repack", "restore_repack_backup"]);
const MERGE_ACTIONS = new Set(["preview_merge", "apply_merge", "apply_offline_merge"]);
const IMPORT_ACTIONS = new Set(["preview_import", "apply_import", "import"]);
const USB_ONLY_ACTIONS = new Set([...REPACK_ACTIONS, ...IMPORT_ACTIONS, ...MERGE_ACTIONS]);
const INSTALLABLE_STATUS = /^NATIVE2 (?:READY [0-9]+|UNPROVISIONED [0-9]+(?: .*)?)$/;
const NEEDS_SELECTION = new Set(["enable", "move", "rename", "delete", "export", "set", "remove", "reset", "preview_optimize",
  "apply_optimize", "open_editor", "preview_editor", "apply_editor"]);
const NEEDS_INSTALLED = new Set(["enable", "set", "remove", "reset", "export", "open_editor", "preview_editor", "apply_editor"]);

const get = (request, key, fallback = null) => (key in request ? request[key] : fallback);
const valueError = (message) => new Error(message);
const isString = (value) => typeof value === "string";
const isDigest = (value) => isString(value) && value.length === 64;

// Python's `x in list` for JSON values (strings compare by value, containers by structure).
const contains = (list, value) => list.some((item) => JSON.stringify(item) === JSON.stringify(value));

export function safeName(value) {
  if (!isString(value) || !value || /[\n\r\0]/.test(value)) throw valueError("Invalid dictionary name");
  return value;
}

function lineAndColumn(source, pos) {
  const before = source.slice(0, pos);
  const lineno = before.split("\n").length;
  const last = before.lastIndexOf("\n");
  return [lineno, last < 0 ? pos + 1 : pos - last];
}

export function readImport(source) {
  let parsed;
  try {
    parsed = parseNamed(source);
  } catch (error) {
    if (error.name === "JSONDecodeError") {
      const [line, column] = lineAndColumn(source, error.pos ?? 0);
      throw valueError(`The dictionary file is not valid JSON (line ${line}, column ${column}).`);
    }
    throw error;
  }
  if (!parsed.entries.length) throw valueError("This JSON dictionary contains zero entries. Choose a file with outline-to-translation entries.");
  return parsed;
}

// json.dumps for the values a request can hold.
function dumps(value) {
  if (isObject(value)) return "{" + Object.entries(value).map(([key, item]) => pyJson(key) + ": " + dumps(item)).join(", ") + "}";
  if (Array.isArray(value)) return "[" + value.map(dumps).join(", ") + "]";
  return pyJson(value);
}

async function using(opener, serial, body) {
  const session = await opener(serial);
  try { return await body(session.console); } finally { await session.close(); }
}

export async function waitReady(serial, {loadState, checking = null, opener}, deps) {
  const clock = deps.clock;
  await clock.sleep(3);
  const started = clock.now();
  let deadline = started + 90, sawBusy = false;
  while (clock.now() < deadline) {
    try {
      const outcome = await using(opener, serial, async (console) => {
        const status = await console.command("native_install status", 2);
        if (status === "ERR Native flash busy" && !sawBusy) {
          sawBusy = true;
          deadline = started + 180;
          if (checking) checking();
        }
        // (Python can also print a listing when no loader is given; the Manager always gives one.)
        if (status.startsWith("NATIVE2 READY ")) return {value: await loadState(console)};
        return null;
      });
      if (outcome) return outcome.value;
    } catch (error) {
      if (!reportable(error)) throw error;
    }
    await clock.sleep(1);
  }
  throw runtimeError("The left did not report READY after reboot; image remains available for diagnosis");
}

export async function setupKeyboard(serial, request, progress, showView, {transport = "usb"}, deps) {
  const opener = transport === "bluetooth" ? deps.bluetoothOpener : deps.opener;
  if (get(request, "approved") !== true) throw valueError("Confirm that setup can clear the external dictionary pool.");
  const target = get(request, "serial");
  if (!isString(target) || !target || (serial && serial !== target)) throw valueError("Reconnect the keyboard and confirm setup for its serial number.");
  const packages = get(request, "packages");
  const expectedStatus = get(request, "expected_status", UNPROVISIONED);
  if (!isString(expectedStatus) || !INSTALLABLE_STATUS.test(expectedStatus)) throw valueError("Open the installer to verify the keyboard before proceeding.");
  if (expectedStatus !== UNPROVISIONED && get(request, "confirm_serial") !== target) throw valueError("Confirm the keyboard serial before replacing the dictionary pool.");
  await using(opener, target, async (console) => {
    if (console.serial !== target || await console.command("native_install status", 30) !== expectedStatus) {
      throw runtimeError("The selected keyboard’s dictionary status changed. Reopen the installer.");
    }
    if (await console.command(FACTORY_CAPABILITIES, 30) !== "OK FACTORY2") throw runtimeError("This keyboard did not offer dictionary installation.");
    if (!Array.isArray(packages) || !packages.length || packages.some((item) => !isString(item) || !(item in FACTORY_CHOICES))
        || new Set(packages).size !== packages.length) throw valueError("Select at least one available factory dictionary.");
    progress("Preparing the selected factory dictionaries…");
    const [image, manifest] = await deps.prepareInstallImage(packages);
    await deps.provision(console, image, manifest, progress, {expected_status: expectedStatus});
  });
  progress("Waiting for the keyboard to reconnect…");
  let view;
  try {
    view = await waitReady(target, {loadState: (console) => loadView(console, "setup", progress, showView, deps), opener}, deps);
  } catch (error) {
    if (isRuntimeError(error)) return {commit_pending: true};
    throw error;
  }
  return {...view, installed: true};
}

export async function runJob(serial, request, progress, showView, {transport = "usb"} = {}, deps) {
  const action = need(request, "action");
  if (transport === "bluetooth" && USB_ONLY_ACTIONS.has(action)) {
    if (MERGE_ACTIONS.has(action)) throw valueError("Connect over USB to merge compact dictionaries.");
    if (IMPORT_ACTIONS.has(action)) throw valueError("Connect over USB to import JSON.");
    throw valueError("Connect over USB to repack or restore a repack backup.");
  }
  const opener = transport === "bluetooth" ? deps.bluetoothOpener : deps.opener;
  const view = (console, name = action) => loadView(console, name, progress, showView, deps);
  if (action === "setup") return setupKeyboard(serial, request, progress, showView, {transport}, deps);
  if (action === "apply_repack" || action === "restore_repack_backup") {
    const restore = action === "restore_repack_backup";
    let source, plan;
    await using(opener, serial, async (console) => {
      serial = console.serial;
      if (!restore) [source, plan] = await deps.captureApprovedRepack(console, request, progress);
    });
    if (restore) await deps.restoreOriginal(serial, request, progress);
    else await deps.installRepack(source, plan, progress);
    return using(opener, serial, async (console) => ({...await view(console), repacked: !restore, restored: restore}));
  }
  if (action === "apply_offline_merge") {
    let source, original;
    await using(opener, serial, async (console) => {
      [source, original] = await deps.captureApprovedMerge(console, request, progress);
      serial = console.serial;
    });
    const name = await deps.installOfflineMerge(source, original, request, progress);
    return using(opener, serial, async (console) => ({...await view(console), merged: name}));
  }

  let published = false;
  let report = {};
  const early = await using(opener, serial, async (console) => {
    serial = console.serial;
    if (action === "open_installer") {
      return {value: {state: await installerView(console, await console.command("native_install status", 5), {reprovision: true, transport})}};
    }
    if (action === "refresh" || action === "layout_map") {
      if (action === "refresh") await deps.clearDocuments(console.serial);
      return {value: await view(console)};
    }
    if (action === "preview_repack") return {value: {repack_plan: await deps.previewRepack(console, progress)}};
    if (action === "preview_compact") {
      let layout = null, generation;
      if (await console.command("native_install update layout-capabilities", 30) === "OK MAP1") {
        try {
          generation = need(parseJson(await console.command("user_dictionary_status", 30)), "generation");
          layout = await readLayout(console);
          layout.images = await imageDetails(console);
        } catch (error) {
          if (!isRuntimeError(error)) throw error;
        }
      }
      const cleanup = await deps.transfer(console, null, progress, {preview_compact: true});
      if (layout && generation !== need(parseJson(await console.command("user_dictionary_status", 30)), "generation")) layout = null;
      if (layout) layout.generation = generation;
      return {value: {cleanup, layout_map: layout}};
    }
    const catalog = await readCatalog(console);
    const byId = new Map(catalog.map((row) => [need(row, "id"), row]));
    const selected = byId.get(get(request, "id")) ?? null;
    if (NEEDS_SELECTION.has(action) && selected === null) throw valueError("Select a dictionary from the current catalog.");
    const name = selected ? safeName(need(selected, "name")) : null;
    if (selected && need(selected, "kind") === "uninstalled" && NEEDS_INSTALLED.has(action)) throw valueError("Install the factory dictionary before using it.");

    if (action === "preview_package" || action === "apply_package") {
      const applying = action === "apply_package";
      const token = get(request, "token"), restore = get(request, "restore");
      if (typeof restore !== "boolean" || (applying && !isDigest(token))) throw valueError("Preview the factory dictionary before applying changes.");
      progress("Checking factory dictionary and flash space…");
      const outcome = await deps.factoryPackage(console, get(request, "package"), {restore, confirmation: applying ? token : null, progress});
      if (!applying) return {value: {package_plan: outcome}};
      report = {package_changed: outcome};
      published = true;
    } else if (action === "enable") {
      const enabled = need(request, "enabled");
      if (typeof enabled !== "boolean") throw valueError("Expected an enabled checkbox value");
      await checked(console, `${enabled ? "enable" : "disable"}_dictionary ${name}`);
    } else if (action === "hints") {
      const mode = get(request, "mode");
      if (mode !== "auto" && mode !== "off") throw valueError("Choose Auto or Off for shorter-outline hints.");
      await checked(console, `stroke_hints ${mode}`);
    } else if (action === "layout") {
      const layout = need(request, "layout");
      if (!contains(parseJson(await console.command("get_parameter available_host_layouts", 30)), layout)) throw valueError("Select a supported host layout.");
      if (!isString(layout)) throw Object.assign(new Error("can only concatenate str (not \"" + typeof layout + "\") to str"), {name: "TypeError"});
      await checked(console, "set_host_layout " + layout);
    } else if (action === "set" || action === "remove" || action === "reset") {
      const outline = need(request, "outline");
      if (!isString(outline)) throw valueError("Enter an outline of up to 16 strokes.");
      const strokes = parseOutline(outline);
      if (!(strokes.length >= 1 && strokes.length <= 16)) throw valueError("Enter an outline of up to 16 strokes.");
      if (action === "reset" && need(selected, "kind") !== "base") throw valueError("Revert to original is available for factory dictionaries.");
      if (action === "set") {
        const {entries, names} = parseNamed(dumps({[outline]: need(request, "translation")}));
        await importEntries(console, need(selected, "id"), entries, names, progress, deps.clock);
      } else {
        await checked(console, `native_${action === "remove" ? "remove" : "reset"}_entry ${need(selected, "id")} ${outline}`);
      }
    } else if (action === "open_editor") {
      progress(`Opening ${name}…`);
      const document = await deps.readDocument(console, selected, progress, {force: get(request, "reload") === true, mode: get(request, "mode", "all")});
      const status = parseJson(await console.command("user_dictionary_status", 30));
      if (!isObject(status)) throw Object.assign(new Error("object has no attribute 'get'"), {name: "AttributeError"});
      return {value: {editor_document: document, editor_read_only: truthy(status.read_only)}};
    } else if (action === "preview_editor" || action === "apply_editor") {
      progress(`Checking ${name} against the keyboard…`);
      const outcome = await deps.changeDocument(console, selected, request, progress, {apply: action === "apply_editor"});
      if (action === "preview_editor") return {value: {editor_plan: outcome}};
      report = {editor_saved: outcome};
    } else if (action === "export") {
      progress(`Exporting ${name}…`);
      const entries = await deps.exportDictionary(console, name);
      return {value: {download: entries, filename: name + ".json", count: entries instanceof Map ? entries.size : Object.keys(entries).length}};
    } else if (action === "backup") {
      progress("Reading saved edits, dictionary order and checkboxes…");
      return {value: {download: await deps.backupAll(console), filename: "stenosquid-backup.json"}};
    } else if (action === "clear_personal") {
      if (get(request, "confirm") !== "CLEAR" || !selected || name !== "user_dictionary") throw valueError("Select your personal dictionary and type CLEAR to confirm.");
      if (await console.command("native_install update snapshot-clear-capabilities", 30) !== "OK CLEAR1") throw runtimeError("Flash the current left firmware before clearing the personal dictionary.");
      progress("Clearing personal entries…");
      published = await deps.clearPersonal(console, need(selected, "id"), progress);
      report = {cleared: true};
    } else if (action === "preview_optimize" || action === "apply_optimize") {
      const applying = action === "apply_optimize";
      const expected = get(request, "token");
      if (applying && !isDigest(expected)) throw valueError("Preview the optimization before staging.");
      const operation = get(request, "operation");
      const eligible = [["personal", "personal"], ["named", "convert"], ["snapshot", "consolidate"]];
      if (!eligible.some(([kind, op]) => need(selected, "kind") === kind && operation === op)) throw valueError("Choose an eligible journal or user snapshot.");
      progress(`Reading ${name} and calculating staging space…`);
      const outcome = await deps.changeSnapshot(console, operation, name, {stage: applying, preview: !applying, expected: applying ? expected : null, target_id: need(selected, "id"), progress});
      if (!applying) return {value: {optimization_plan: outcome}};
      published = outcome;
      report = {optimized: name};
    } else if (MERGE_ACTIONS.has(action)) {
      const applying = action === "apply_merge";
      const token = get(request, "token");
      if (applying && !isDigest(token)) throw valueError("Preview the merge before staging.");
      const result = await deps.mergeSnapshots(console, get(request, "upper"), get(request, "lower"), {expected: applying ? token : null, progress});
      if (!applying) return {value: {merge_plan: result}};
      published = true;
      report = {merged: result};
    } else if (action === "preview_import" || action === "apply_import") {
      const {entries} = readImport(need(request, "source"));
      const staging = action === "apply_import";
      const expected = get(request, "token");
      if (staging && !isDigest(expected)) throw valueError("Preview the import before staging.");
      let importReport;
      try {
        importReport = await deps.namedImport(console, safeName(need(request, "name")), entries, {stage: staging, expected, progress});
      } catch (error) {
        // Python catches only ValueError here, not RuntimeError.
        if (error.name === "Error" && !isRuntimeError(error) && error.message === "The keyboard has reached its dictionary limit" && await mergeLimit(console)) {
          throw valueError("The keyboard has reached its dictionary limit. Choose Merge compact dictionaries above to free a slot.");
        }
        throw error;
      }
      if (!staging) return {value: {import_plan: importReport}};
      published = true;
      report = {imported: entries.length};
    } else if (["move", "rename", "delete", "import", "compact"].includes(action)) {
      if ((action === "rename" || action === "delete") && (name === "user_dictionary" || !["named", "snapshot"].includes(need(selected, "kind")))) {
        throw valueError("Rename and delete are available for added dictionaries.");
      }
      if (await console.command("native_install update manage-capabilities", 30) !== "OK MANAGE1") throw runtimeError("Flash the matching Manager firmware before writing.");
      const options = {};
      let entries = null;
      if (action === "compact") {
        const token = get(request, "token");
        if (!isDigest(token)) throw valueError("Preview journal cleanup before compacting.");
        options.expected_compact = token;
      } else if (action === "move") {
        options.move = [name, pyInt(need(request, "position"))];
      } else if (action === "rename") {
        options.rename = [name, safeName(need(request, "name"))];
      } else if (action === "delete") {
        if (get(request, "confirm") !== name) throw valueError("Confirm the name of the dictionary to delete.");
        options.delete = name;
      } else if (action === "import") {
        ({entries} = readImport(need(request, "source")));
        if (truthy(get(request, "name"))) throw valueError("Preview named imports before saving the compact dictionary.");
      }
      if (action === "delete" && need(selected, "kind") === "snapshot") published = await deps.changeSnapshot(console, "delete", name, {stage: true, progress});
      else published = await deps.transfer(console, entries, progress, options);
      if (action === "import") report = {imported: entries.length};
    } else {
      throw valueError("Unknown Manager operation");
    }
    return null;
  });
  if (early) return early.value;

  if (truthy(published)) {
    progress("Keyboard acknowledged the catalog commit. Waiting for it to reconnect…");
    let state;
    try {
      state = await waitReady(serial, {loadState: (console) => loadView(console, action, progress, showView, deps),
        checking: () => progress("Saved. Keyboard is checking dictionaries…"), opener}, deps);
    } catch (error) {
      if (isRuntimeError(error)) return {commit_pending: true};
      throw error;
    }
    return {...state, ...report};
  }
  return using(opener, serial, async (console) => ({...await view(console), ...report}));
}

// int() as Python applies it to a JSON value: integers and integral floats, bools, and decimal strings.
function pyInt(value) {
  if (typeof value === "boolean") return value ? 1 : 0;
  if (typeof value === "number") {
    if (!Number.isFinite(value)) throw valueError("cannot convert float infinity or NaN to integer");
    return Math.trunc(value);
  }
  if (isString(value)) {
    const text = value.trim();
    if (!/^[+-]?[0-9]+$/.test(text)) throw valueError(`invalid literal for int() with base 10: ${pyRepr(value)}`);
    return Number(text);
  }
  throw Object.assign(new Error("int() argument must be a string, a bytes-like object or a real number"), {name: "TypeError"});
}

