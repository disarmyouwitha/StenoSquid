// Manager milestone M3: reading the keyboard's state for the page, ported from tools/manager/manager.py (read_state,
// load_view, installer_view, read_catalog, scan_layout, image_details) and overlay_usage in tools/storage/native_update.py.
// `console` is the vendor console (console.js or a fake). `deps` carries what the browser supplies:
//   lastRepackBackup(serial) -> name | null     (the recovery folder's latest repack backup; USB only)
//   sleep(seconds) -> Promise                   (the layout-scan retry wait)
// golden_state.py supplies the golden vectors.
import {SEGMENT} from "./snapshot.js";
import {readSnapshot, updateCommand} from "./device_reads.js";
import {readImages, readLayout} from "./layout.js";
import {FACTORY_KEYS} from "./package_plan.js";
import {pyRepr} from "./importer.js";

export const UNPROVISIONED = "NATIVE2 UNPROVISIONED 0";
export const FACTORY_CAPABILITIES = "native_install update factory-member-capabilities";
export const LARGE_JOURNAL_RECORDS = 4096;
export const FACTORY_CHOICES = Object.fromEntries(Object.entries(FACTORY_KEYS).map(([key, row]) => [key, row[1]]));
export const FACTORY_ACTIONS = Object.fromEntries(Object.entries(FACTORY_KEYS).map(([key, row]) => [String(row[0]), key]));
export const MAP_REFRESH_ACTIONS = new Set(["refresh", "layout_map", "setup", "import", "apply_import", "compact", "apply_package",
  "apply_optimize", "clear_personal", "delete", "set", "remove", "reset", "apply_repack", "restore_repack_backup", "apply_merge",
  "apply_offline_merge", "apply_editor"]);
const INSTALLABLE_STATUS = /^NATIVE2 (?:READY [0-9]+|UNPROVISIONED [0-9]+(?: .*)?)$/;

// Python's truthiness for the JSON values a status reply can hold.
export function truthy(value) {
  if (value === null || value === undefined || value === false || value === 0 || value === "") return false;
  if (Array.isArray(value)) return value.length > 0;
  if (typeof value === "object") return Object.keys(value).length > 0;
  return true;
}
const keyError = (key) => Object.assign(new Error(`'${key}'`), {name: "KeyError"});
const need = (object, key) => { if (!(key in object)) throw keyError(key); return object[key]; };
const isObject = (value) => value !== null && typeof value === "object" && !Array.isArray(value);
// Python's `except (RuntimeError, OSError, ValueError)`: the failures the page reports instead of aborting on.
export const reportable = (error) => ["Error", "TimeoutError", "JSONDecodeError", "UnicodeDecodeError", "UnicodeEncodeError"].includes(error.name);

function parseJson(text) {
  try { return JSON.parse(text); } catch (error) { throw Object.assign(new Error(error.message), {name: "JSONDecodeError"}); }
}

export async function mergeLimit(console) {
  const capability = (await console.command("native_install update snapshot-merge-capabilities", 30)).trim().split(/\s+/);
  if (capability.length === 3 && capability[0] === "OK" && capability[1] === "MERGE1" && /^[0-9]+$/.test(capability[2])) {
    const limit = Number(capability[2]);
    if (limit >= 7 && limit <= 64) return limit;
  }
  return null;
}

export async function installerView(console, status, {reprovision = false, transport = "usb"} = {}) {
  if (!INSTALLABLE_STATUS.test(status)) throw new Error("The dictionary installer is unavailable. Keep the left half connected and try again.");
  if (await console.command(FACTORY_CAPABILITIES, 30) !== "OK FACTORY2") throw new Error("This keyboard did not offer dictionary installation.");
  return {transport, setup: {serial: console.serial, packages: FACTORY_CHOICES, expected_status: status, reprovision}};
}

export async function readCatalog(console) {
  const value = parseJson(await console.command("native_catalog", 30));
  if (!Array.isArray(value) || !value.length) throw new Error("Provision a keyboard with the matching Steno Squid firmware first.");
  return value;
}

export async function imageDetails(console) {
  try { return await readImages(console); } catch (error) { if (reportable(error)) return null; throw error; }
}

export async function scanLayout(console, progress, deps) {
  for (let attempt = 0; attempt < 3; attempt++) {
    try {
      return await readLayout(console);
    } catch (error) {
      // Only a RuntimeError reply can be deferred; anything else (a malformed map) is final.
      if (error.name !== "Error" || !error.message.startsWith("ERR Layout scan deferred") || attempt === 2) throw error;
      progress("Waiting for flash, then retrying the scan…");
      if (error.message === "ERR Layout scan deferred until the flash and input are idle") {
        // Earlier firmware wakes flash for status queries but not for the map.
        await console.command("native_install status", 3);
      }
      await deps.sleep(0.25);
    }
  }
}

export async function loadLayout(console, progress, deps) {
  const layout = await scanLayout(console, progress, deps);
  layout.images = await imageDetails(console);
  return layout;
}

const intOf = (token) => {
  if (!/^[+-]?[0-9]+$/.test(token)) throw new Error(`invalid literal for int() with base 10: ${pyRepr(token)}`);
  return Number(token);
};

export async function overlayUsage(console, {metadataOnly = false} = {}) {
  const capability = await console.command("native_install update journal-usage-capabilities", 30);
  if (capability === "ERR Native flash busy") throw new Error(capability);
  const onDevice = capability === "OK USAGE2";
  if (metadataOnly && !onDevice) throw new Error("This keyboard does not offer a metadata-only journal measurement.");
  const answer = await updateCommand(console, onDevice ? "begin metadata" : "begin backup");
  try {
    const {snapshot} = await readSnapshot(console, answer, {metadataOnly: onDevice});
    const usage = new Map();
    for (const row of snapshot.rows) {
      usage.set(new DataView(row.buffer, row.byteOffset, row.byteLength).getUint32(0, true), {count: 0, entries: 0, bytes: 0, blocks: new Map()});
    }
    const add = (segment, identity, count, entries, bytesUsed) => {
      const stats = usage.get(identity);
      stats.count += count;
      stats.entries += entries;
      stats.bytes += bytesUsed;
      stats.blocks.set(segment, (stats.blocks.get(segment) ?? 0) + bytesUsed);
    };
    if (onDevice) {
      const journals = new Set();
      for (const [start, span, , , kind] of snapshot.extents) {
        if (Number(kind) === 2) for (let segment = Number(start); segment < Number(start) + Number(span); segment++) journals.add(segment);
      }
      let position = 0, total = null;
      while (total === null || position < total) {
        const parts = (await updateCommand(console, `journal-usage ${position}`)).trim().split(/\s+/);
        if (parts.length < 4) throw new Error(`not enough values to unpack (expected at least 4, got ${parts.length})`);
        const [marker, version, cursorText, limitText, ...rows] = parts;
        const cursor = intOf(cursorText), limit = intOf(limitText);
        if (marker !== "OK" || version !== "USAGE2" || !(position <= cursor && cursor <= limit) || cursor - position > 128
            || (total !== null && total !== limit) || (cursor === position && cursor !== limit)) throw new Error("Invalid journal usage cursor");
        let counts = 0;
        const seen = new Set();
        for (const row of rows) {
          const tokens = row.split(",");
          const values = [];
          for (let i = 0; i < Math.min(tokens.length, 6); i++) values.push(intOf(tokens[i]));
          if (values.length < 5) throw new Error(`not enough values to unpack (expected 5, got ${values.length})`);
          if (values.length > 5) throw new Error("too many values to unpack (expected 5)");
          const [segment, identity, count, entries, bytesUsed] = values;
          const pair = `${segment},${identity}`;
          if (!journals.has(segment) || !usage.has(identity) || seen.has(pair) || !(entries >= 0 && entries <= count)
              || !(count > 0 && count <= cursor - position) || !(bytesUsed > 0 && bytesUsed <= SEGMENT)) throw new Error("Invalid journal usage totals");
          seen.add(pair);
          counts += count;
          add(segment, identity, count, entries, bytesUsed);
        }
        if (counts > cursor - position) throw new Error("Invalid journal usage count");
        position = cursor;
        total = limit;
      }
    } else {
      for (const entry of snapshot.entries.values()) add(Math.floor(entry.address / SEGMENT), entry.identity, 1, entry.operation === 0 ? 1 : 0, entry.span);
    }
    // The page receives JSON: object keys are strings, in the order Python's dict held them.
    const out = {};
    for (const [identity, stats] of usage) {
      out[identity] = {count: stats.count, entries: stats.entries, bytes: stats.bytes,
        blocks: Object.fromEntries([...stats.blocks].map(([segment, used]) => [segment, used]))};
    }
    return out;
  } finally {
    await updateCommand(console, "abort", 5);
  }
}

export async function readState(console, deps) {
  const transport = console.transport ?? "usb";
  const installStatus = await console.command("native_install status", 30);
  if (installStatus === "ERR Native flash busy") throw new Error("The keyboard is still checking dictionary flash. Try connecting again in a moment.");
  if (installStatus.startsWith("NATIVE2 UNPROVISIONED ")) {
    const backup = transport === "usb" ? await deps.lastRepackBackup(console.serial) : null;
    if (backup && await console.command("native_install update recovery-capabilities", 30) === "OK RECOVERY1") {
      return {transport, setup: {serial: console.serial, repack_backup: backup}};
    }
    if (installStatus === UNPROVISIONED) return installerView(console, installStatus, {transport});
    throw new Error("The keyboard could not open its dictionary store. Use USB recovery or explicit reprovisioning.");
  }
  if (installStatus.startsWith("NATIVE2 INSTALL ")) throw new Error("A dictionary install is in progress. Keep the keyboard connected and refresh when it finishes.");
  if (!installStatus.startsWith("NATIVE2 READY ")) throw new Error("Connect the left half running Steno Squid Native firmware.");
  let status;
  try {
    status = parseJson(await console.command("user_dictionary_status", 30));
  } catch (error) {
    if (error.name === "JSONDecodeError") throw new Error("The dictionary store did not respond. Refresh the connection and try again.");
    throw error;
  }
  if (!isObject(status) || status.format !== "SQBCAT02" || !truthy(status.ready)) throw new Error("The dictionary store is unavailable. Check the connection and firmware.");
  const layouts = parseJson(await console.command("get_parameter available_host_layouts", 30));
  const adaptive = await console.command("native_install update snapshot-capabilities", 30) === "OK SNAPSHOT2";
  const catalog = await readCatalog(console);
  const factory = await console.command(FACTORY_CAPABILITIES, 30) === "OK FACTORY2";
  const hints = await console.command("stroke_hints", 30);
  // The page's state is built in Python dict order: the host layout is asked for before a missing status key is reported.
  const layout = await console.command("get_parameter host_layout", 30);
  const readOnly = need(status, "read_only");
  const writable = adaptive && !truthy(readOnly);
  const state = {serial: console.serial, transport, catalog, layouts, layout,
    hints: hints === "auto" || hints === "off" ? hints : null, read_only: readOnly, auto_checkpoints: status.auto_checkpoints ?? 0,
    auto_reclaimed: status.auto_reclaimed ?? 0, tail: need(status, "tail"), generation: status.generation ?? null,
    maintenance_active: truthy(status.maintenance)};
  state.personal_conversion = adaptive && await console.command("native_install update personal-snapshot-capabilities", 30) === "OK PERSONAL1";
  state.journal_conversion = adaptive && await console.command("native_install update journal-snapshot-capabilities", 30) === "OK JOURNAL1";
  state.personal_clear = adaptive && await console.command("native_install update snapshot-clear-capabilities", 30) === "OK CLEAR1";
  state.adaptive_imports = adaptive;
  state.merge_limit = writable ? await mergeLimit(console) : null;
  state.factory_actions = factory ? FACTORY_ACTIONS : {};
  state.repack = transport === "usb" && writable && await console.command("native_install update repack-capabilities", 30) === "OK REPACK1";
  state.layout_map = adaptive && await console.command("native_install update layout-capabilities", 30) === "OK MAP1";
  return state;
}

export async function loadView(console, action, progress, showView, deps) {
  const state = await readState(console, deps);
  const result = {state};
  if (truthy(state.layout_map) && MAP_REFRESH_ACTIONS.has(action)) {
    try {
      progress("Scanning memory…");
      result.layout_map = await loadLayout(console, progress, deps);
      result.layout_map.generation = need(state, "generation");
    } catch (error) {
      if (!reportable(error)) throw error;
      result.layout_map_error = error.message;
    }
  }
  if (truthy(state.catalog) && MAP_REFRESH_ACTIONS.has(action)) {
    if (showView) showView({...result});
    try {
      progress("Measuring journal sizes…");
      const usage = await overlayUsage(console);
      if ("layout_map" in result) {
        const expected = `NATIVE2 READY ${state.generation === null ? "None" : state.generation}`;
        if (await console.command("native_install status", 30) === expected) result.layout_map.journal_usage = usage;
      }
      result.usage = usage;
      result.journal_budget_exceeded = Object.values(usage).reduce((sum, row) => sum + row.count, 0) >= LARGE_JOURNAL_RECORDS;
    } catch (error) {
      if (!reportable(error)) throw error;
      result.usage_error = error.message;
    }
  }
  return result;
}
