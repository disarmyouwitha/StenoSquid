// Manager milestone M3: writing single entries through the console, ported from tools/storage/native_manage.py
// (checked, commit_with_progress, import_entries). `clock` is {now() -> seconds, sleep(seconds) -> Promise}.
// golden_jobs.py records these inside the job transcripts (set / remove / reset actions).
import {runtimeError} from "./errors.js";
import {reportable, truthy} from "./state.js";

export async function checked(console, line) {
  const result = await console.command(line, 60);
  if (result !== "OK") throw runtimeError(result);
}

const same = (a, b) => a !== null && a.length === b.length && a.every((value, i) => value === b[i]);

// Wait on live maintenance, with both a stall limit and a total limit.
export async function commitWithProgress(console, progress, clock) {
  const start = clock.now();
  const totalDeadline = start + 3600;
  let stallDeadline = start + 180, heartbeat = start, previous = null, announced = false;
  for (;;) {
    const response = await console.command("native_entry commit", 60);
    if (response === "OK") return;
    const now = clock.now();
    if (!response.startsWith("ERR Native store busy")) throw runtimeError(response);
    if (now >= totalDeadline || now >= stallDeadline) throw runtimeError("Native maintenance timed out without a committed entry");
    if (now >= heartbeat) {
      let status;
      try { status = JSON.parse(await console.command("user_dictionary_status", 60)); }
      catch (error) { if (error instanceof SyntaxError) throw Object.assign(new Error(error.message), {name: "JSONDecodeError"}); throw error; }
      if (status === null || typeof status !== "object" || Array.isArray(status)) throw Object.assign(new Error("'dict' object expected"), {name: "AttributeError"});
      if (status.format !== "SQBCAT02" || !truthy(status.ready) || truthy(status.read_only)) throw runtimeError("Native store became unavailable or read-only");
      const current = ["generation", "work_steps", "tail"].map((key) => status[key] ?? null);
      if (!same(previous, current)) { previous = current; stallDeadline = now + 180; }
      heartbeat = now + 1;
      if (!announced) { progress("Waiting for dictionary maintenance; checking progress…"); announced = true; }
    }
    await clock.sleep(0.05);
  }
}

function toBase64(bytes) {
  let text = "";
  for (let i = 0; i < bytes.length; i++) text += String.fromCharCode(bytes[i]);
  return btoa(text);
}

// entries: [[strokes[], Uint8Array]] in order; names: [[strokes[], outline]].
export async function importEntries(console, identity, entries, names, progress, clock) {
  await checked(console, "native_entry abort");
  const outlineOf = new Map(names.map(([strokes, outline]) => [strokes.join(","), outline]));
  let number = 0;
  for (const [strokes, text] of entries) {
    number++;
    const outline = outlineOf.get(strokes.join(","));
    try {
      await checked(console, `native_entry begin ${identity} ${text.length} ${outline}`);
      for (let offset = 0; offset < text.length; offset += 512) {
        await checked(console, `native_entry data ${offset} ${toBase64(text.subarray(offset, offset + 512))}`);
      }
      await commitWithProgress(console, progress, clock);
    } catch (error) {
      if (!reportable(error)) throw error;
      throw runtimeError(`Stopped at ${outline} after ${number - 1} saved entries: ${error.message}`);
    }
    if (number % 100 === 0) progress(`Saved ${number}/${entries.length} entries`);
  }
  progress(`Saved ${entries.length} entries in dictionary ${identity}`);
}
