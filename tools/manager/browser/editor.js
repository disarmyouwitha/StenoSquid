// Manager: guarded JSON editing of one effective dictionary and its saved overrides, ported from tools/manager/manager_editor.py
// (parse_source, parse_edits, fingerprint, effective_document, saved_edits, read_document, change_document, clear_documents).
// golden_editor.py runs the Python on scripted keyboard sessions and records every console command, progress line, factory-JSON
// lookup and step result; the JS must reproduce them step for step.
//
//   makeEditor({factorySource, clock}) -> {readDocument, changeDocument, clearDocuments}
// factorySource(identity, name, sha256) -> Map(outline -> text) | null: the bundled factory JSON for a pinned dictionary when its
// hash matches (the Python read it from build/; the page fetches it). clock is {now(), sleep(seconds)} for the entry commits.
import {parseNamed, parseOrderedJson, isOrderedObject} from "./importer.js";
import {exportDictionary} from "./export.js";
import {updateCommand, readSnapshot} from "./device_reads.js";
import {rowName} from "./snapshot.js";
import {formatStroke} from "./decode.js";
import {checked, importEntries} from "./manage.js";
import {sha256Hex, pyJson} from "./snapshot_plan.js";
import {parseJson, getDefault, need, truthy, isObject, reportable} from "./state.js";
import {lineAndColumn} from "./jobs.js";
import {runtimeError, isRuntimeError} from "./errors.js";
import {dumps} from "./recovery.js";

const valueError = (message) => new Error(message);
const utf8 = (text) => new TextEncoder().encode(text);
const decode = (bytes) => new TextDecoder("utf-8", {fatal: true}).decode(bytes);
const sameBytes = (a, b) => a.length === b.length && a.every((value, i) => value === b[i]);
const sameValue = (a, b) => (a === null || b === null ? a === b : sameBytes(a, b));
const strokeKey = (strokes) => strokes.join(",");
const compareStrokes = (a, b) => {
  for (let i = 0; i < Math.min(a.length, b.length); i++) if (a[i] !== b[i]) return a[i] - b[i];
  return a.length - b.length;
};
const pyIntLike = (value) => typeof value === "number" && Number.isInteger(value);
const sameJson = (a, b) => JSON.stringify(a ?? null) === JSON.stringify(b ?? null);

// `status.get(...)` on something that is not an object is an AttributeError in Python.
function statusObject(text) {
  const value = parseJson(text);
  if (!isObject(value)) {
    const type = value === null ? "NoneType" : Array.isArray(value) ? "list" : typeof value === "string" ? "str" : typeof value === "boolean" ? "bool" : Number.isInteger(value) ? "int" : "float";
    throw Object.assign(new Error(`'${type}' object has no attribute 'get'`), {name: "AttributeError"});
  }
  return value;
}

// A JSON syntax problem as the editor reports it (Python's wording of the decoder's reason is not a contract).
function jsonProblem(source, error) {
  if (error.name !== "JSONDecodeError") return error;
  const [line, column] = lineAndColumn(source, error.pos ?? 0);
  return valueError(`Invalid JSON at line ${line}, column ${column}: ${String(error.message).replace(/: char \d+$/, "")}`);
}

function parseSource(source) {
  if (typeof source !== "string") throw valueError("Enter a JSON dictionary object.");
  try {
    return parseNamed(source);
  } catch (error) {
    throw jsonProblem(source, error);
  }
}

function parseEdits(source) {
  if (typeof source !== "string") throw valueError("Enter a JSON object of saved edits.");
  let data;
  try {
    data = parseOrderedJson(source);
  } catch (error) {
    throw jsonProblem(source, error);
  }
  if (!isOrderedObject(data)) throw valueError("Saved edits must be a JSON object mapping outlines to translations or null.");
  const entries = new Map(), names = new Map();
  for (const [outline, text] of data.pairs) {
    // A null reset is checked as an empty translation; any other non-text value fails the text check just the same.
    const probe = text === null ? "" : typeof text === "string" ? text : 0;
    const parsed = parseSource(`{${pyJson(outline)}: ${pyJson(probe)}}`);
    const [strokes, bytes] = parsed.entries[0];
    const value = text === null ? null : bytes;
    const key = strokeKey(strokes);
    if (entries.has(key) && !sameValue(entries.get(key)[1], value)) throw valueError("Two JSON keys describe the same outline with different edits.");
    if (!entries.has(key)) entries.set(key, [strokes, value]); else entries.get(key)[1] = value;
    if (!names.has(key)) names.set(key, [strokes, parsed.names[0][1]]);
  }
  return {entries: [...entries.values()], names: [...names.values()]};
}

function editorMode(mode) {
  if (mode !== "all" && mode !== "edits") throw valueError("Choose the whole dictionary or saved edits.");
  return mode;
}

async function fingerprint(selected, serial, entries, mode = "all") {
  const rows = [...entries].sort((a, b) => compareStrokes(a[0], b[0])).map(([strokes, text]) => [[...strokes], text === null ? null : decode(text)]);
  return sha256Hex(utf8(dumps([serial ?? null, need(selected, "id"), need(selected, "name"), need(selected, "kind"), mode, rows])));
}

// json.dumps(value, ensure_ascii=False, indent=2) for an outline -> text|null mapping.
function jsonText(text) {
  let out = '"';
  for (let i = 0; i < text.length; i++) {
    const ch = text[i], code = text.charCodeAt(i);
    out += ch === '"' ? '\\"' : ch === "\\" ? "\\\\" : code === 10 ? "\\n" : code === 13 ? "\\r" : code === 9 ? "\\t" : code === 8 ? "\\b"
      : code === 12 ? "\\f" : code < 32 ? "\\u" + code.toString(16).padStart(4, "0") : ch;
  }
  return out + '"';
}
export function dumpsIndented(entries) {
  const lines = [...entries].map(([outline, text]) => `  ${jsonText(outline)}: ${text === null ? "null" : jsonText(text)}`);
  return lines.length ? "{\n" + lines.join(",\n") + "\n}" : "{}";
}

// f"{received / 1024:,.1f}": one decimal, rounded half to even, thousands separators.
export function kibibytes(received) {
  const tenths10 = received * 10;
  let q = Math.floor(tenths10 / 1024);
  const r = tenths10 % 1024;
  if (r > 512 || (r === 512 && q % 2 === 1)) q++;
  const whole = Math.floor(q / 10);
  return `${String(whole).replace(/\B(?=(\d{3})+(?!\d))/g, ",")}.${q % 10}`;
}

const rowIdentity = (row) => new DataView(row.buffer, row.byteOffset, row.byteLength).getUint32(0, true);
const rowBase = (row) => new DataView(row.buffer, row.byteOffset, row.byteLength).getUint16(8, true);
const hex = (bytes) => Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");

export function makeEditor({factorySource, clock}) {
  const documents = new Map();  // key -> {prefix, document}, in first-insertion order like Python's dict

  function clearDocuments(serial) {
    for (const [key, entry] of [...documents]) if (entry.serial === serial) documents.delete(key);
  }

  async function baselineFor(selected) {
    return factorySource(need(selected, "id"), need(selected, "name"), getDefault(selected, "source_sha256", null));
  }

  async function effectiveDocument(console, selected, onRead, progress, onSnapshot) {
    const baseline = need(selected, "kind") === "base" ? await baselineFor(selected) : null;
    if (baseline === null) {
      progress("Reading the dictionary from the keyboard…");
      return exportDictionary(console, need(selected, "name"), {onRead, onSnapshot, trimJournals: true});
    }
    progress("Opening bundled factory JSON; reading saved changes from the keyboard…");
    const answer = await updateCommand(console, "begin backup");
    try {
      const {snapshot} = await readSnapshot(console, answer, {onRead, trimJournals: true});
      const row = snapshot.rows.find((candidate) => rowIdentity(candidate) === selected.id);
      if (row === undefined || rowName(row) !== selected.name || row[11] !== 0 || rowBase(row) === 65535
          || hex(row.subarray(64, 96)) !== selected.source_sha256) {
        throw valueError("The factory dictionary changed. Reconnect and reopen the editor.");
      }
      if (onSnapshot !== null) await onSnapshot(snapshot);
      const result = new Map(baseline);
      for (const entry of snapshot.entries.values()) {
        if (entry.identity !== selected.id) continue;
        const outline = entry.strokes.map(formatStroke).join("/");
        if (entry.operation === 1) result.delete(outline);
        else if (entry.operation === 0) result.set(outline, decode(entry.text));
      }
      return result;
    } finally {
      await updateCommand(console, "abort", 5);
    }
  }

  function snapshotEdits(snapshot, selected) {
    const row = snapshot.rows.find((candidate) => rowIdentity(candidate) === need(selected, "id"));
    if (row === undefined || rowName(row) !== need(selected, "name") || row[11] === 2) throw valueError("The dictionary changed. Reconnect and reopen the editor.");
    const result = new Map();
    for (const entry of snapshot.entries.values()) {
      if (entry.identity === selected.id && (entry.operation === 0 || entry.operation === 1)) {
        result.set(entry.strokes.map(formatStroke).join("/"), entry.operation === 1 ? null : decode(entry.text));
      }
    }
    return result;
  }

  async function savedEdits(console, selected, onRead, progress, onSnapshot) {
    progress("Reading saved edits from the keyboard…");
    const answer = await updateCommand(console, "begin backup");
    try {
      const {snapshot} = await readSnapshot(console, answer, {onRead, trimJournals: true});
      const result = snapshotEdits(snapshot, selected);
      if (onSnapshot !== null) await onSnapshot(snapshot);
      return result;
    } finally {
      await updateCommand(console, "abort", 5);
    }
  }

  async function makeDocument(console, selected, entries, mode) {
    const source = dumpsIndented(entries) + "\n";
    const parsed = (mode === "edits" ? parseEdits : parseSource)(source);
    return {id: need(selected, "id"), name: need(selected, "name"), kind: need(selected, "kind"), source, count: parsed.entries.length, mode,
      version: await fingerprint(selected, console.serial, parsed.entries, mode)};
  }

  async function readDocument(console, selected, progress = () => {}, {force = false, mode = "all"} = {}) {
    mode = editorMode(mode);
    const status = statusObject(await console.command("user_dictionary_status"));
    let prefix = null;
    if (truthy(getDefault(status, "ready", null)) && pyIntLike(getDefault(status, "generation", null)) && pyIntLike(getDefault(status, "tail", null))) {
      prefix = JSON.stringify([console.serial ?? null, need(selected, "id"), need(selected, "name"), need(selected, "kind"), getDefault(selected, "source_sha256", null),
        getDefault(selected, "image_crc", null), getDefault(selected, "image_bytes", null), status.generation, status.tail]);
      const hit = documents.get(`${prefix}|${mode}`);
      if (!force && hit) return {...hit.document};
    }
    let received = 0, reported = -1;
    const onRead = (count) => {
      received += count;
      const bucket = Math.floor(received / 8192);
      if (bucket !== reported) {
        reported = bucket;
        progress(`Reading keyboard data: ${kibibytes(received)} KiB…`);
      }
    };
    const related = new Map();
    const capture = async (snapshot) => {
      const edits = snapshotEdits(snapshot, selected);
      related.set("edits", edits);
      if (mode === "all") return;
      const row = snapshot.rows.find((candidate) => rowIdentity(candidate) === selected.id);
      let baseline = null;
      if (row[11] === 0 && rowBase(row) === 65535) baseline = new Map();
      else if (selected.kind === "base" && hex(row.subarray(64, 96)) === getDefault(selected, "source_sha256", null)) baseline = await baselineFor(selected);
      if (baseline !== null) {
        const complete = new Map(baseline);
        for (const [outline, text] of edits) {
          if (text === null) complete.delete(outline); else complete.set(outline, text);
        }
        related.set("all", complete);
      }
    };
    const entries = await (mode === "edits" ? savedEdits : effectiveDocument)(console, selected, onRead, progress, capture);
    const after = statusObject(await console.command("user_dictionary_status"));
    if (["generation", "tail"].some((field) => !sameJson(getDefault(status, field, null), getDefault(after, field, null)))) {
      throw valueError("The dictionary changed while loading. Reopen the editor.");
    }
    progress("Preparing JSON for the editor…");
    const document = await makeDocument(console, selected, entries, mode);
    if (prefix !== null) {
      related.set(mode, entries);
      if (force) for (const [key, entry] of [...documents]) if (entry.prefix === prefix) documents.delete(key);
      for (const [relatedMode, data] of related) {
        const relatedKey = `${prefix}|${relatedMode}`;
        if (!documents.has(relatedKey) && documents.size >= 6) documents.delete(documents.keys().next().value);
        documents.set(relatedKey, {prefix, serial: console.serial ?? null, document: {...(relatedMode === mode ? document : await makeDocument(console, selected, data, relatedMode))}});
      }
    }
    return document;
  }

  async function changeDocument(console, selected, request, progress, {apply = false} = {}) {
    const mode = editorMode(getDefault(request, "mode", "all"));
    const parse = mode === "edits" ? parseEdits : parseSource;
    const desired = parse(getDefault(request, "source", null));
    const current = await readDocument(console, selected, progress, {mode});
    if (getDefault(request, "version", null) !== current.version) {
      throw valueError("This dictionary changed since you opened it. Copy your draft before reloading from the keyboard.");
    }
    const original = parse(current.source);
    const want = new Map(desired.entries.map(([strokes, value]) => [strokeKey(strokes), [strokes, value]]));
    const had = new Map(original.entries.map(([strokes, value]) => [strokeKey(strokes), [strokes, value]]));
    const added = [...want.keys()].filter((key) => !had.has(key));
    const updated = [...want.keys()].filter((key) => had.has(key) && !sameValue(want.get(key)[1], had.get(key)[1]));
    const removed = [...had.keys()].filter((key) => !want.has(key));
    const version = await fingerprint(selected, console.serial, desired.entries, mode);
    const token = await sha256Hex(utf8(current.version + version));
    const plan = {added: added.length, updated: updated.length, removed: removed.length, count: want.size, mode, token};
    if (!apply) return plan;
    if (getDefault(request, "token", null) !== token) throw valueError("Preview these JSON changes before saving them.");
    const status = statusObject(await console.command("user_dictionary_status"));
    if (!truthy(getDefault(status, "ready", null)) || truthy(getDefault(status, "read_only", null))) throw valueError("The dictionary store is unavailable or read-only.");
    if (mode === "edits" && removed.length && selected.kind === "snapshot") {
      if (await console.command("native_install update snapshot-reset-capabilities") !== "OK RESET1") {
        throw valueError("Update the left firmware to undo saved edits on an added compact dictionary.");
      }
    }
    const total = added.length + updated.length + removed.length;
    let saved = 0;
    const byStrokes = (keys, table) => keys.map((key) => table.get(key)).sort((a, b) => compareStrokes(a[0], b[0]));
    const names = new Map(desired.names.map(([strokes, outline]) => [strokeKey(strokes), outline]));
    try {
      for (const [strokes, value] of byStrokes([...added, ...updated], want)) {
        if (value === null) await checked(console, `native_remove_entry ${selected.id} ${names.get(strokeKey(strokes))}`);
        else await importEntries(console, selected.id, [[strokes, value]], desired.names, () => {}, clock);
        saved++;
        progress(`Saving dictionary changes: ${saved}/${total}`);
      }
      const originalNames = new Map(original.names.map(([strokes, outline]) => [strokeKey(strokes), outline]));
      for (const [strokes] of byStrokes(removed, had)) {
        const verb = mode === "edits" ? "reset" : "remove";
        await checked(console, `native_${verb}_entry ${selected.id} ${originalNames.get(strokeKey(strokes))}`);
        saved++;
        progress(`Saving dictionary changes: ${saved}/${total}`);
      }
    } catch (error) {
      if (!reportable(error)) throw error;
      try {
        await checked(console, "native_entry abort");
      } catch (inner) {
        if (!isRuntimeError(inner) && inner.name !== "TimeoutError") throw inner;  // Python: except (RuntimeError, OSError, TimeoutError)
      }
      throw runtimeError(`Saved ${saved}/${total} changes before stopping: ${error.message}. Copy your draft before reloading from the keyboard and retrying.`);
    }
    const data = new Map(desired.entries.map(([strokes, value]) => [names.get(strokeKey(strokes)), value === null ? null : decode(value)]));
    return {...current, source: dumpsIndented(data) + "\n", version, count: want.size, changes: total};
  }

  return {readDocument, changeDocument, clearDocuments};
}
