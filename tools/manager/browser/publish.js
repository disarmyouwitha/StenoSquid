// Publishing a staged update to the keyboard, ported from tools/storage/native_update.py (publish, abort, transfer).
// publish() prefers the binary transfer session and falls back to Base64 console writes when the keyboard does not
// offer it (or refuses before any data moved). transfer() is the whole move / rename / delete / import / compact job.
import {runtimeError} from "./errors.js";
import {updateCommand, readSnapshot} from "./device_reads.js";
import {plan} from "./plan.js";
import {upload, TransferUnavailable, linkFromHid} from "./transfer_client.js";
import {crc32} from "./transfer_frame.js";
import {sha256Hex} from "./snapshot_plan.js";
import {SEGMENT} from "./snapshot.js";

const utf8 = (text) => new TextEncoder().encode(text);

function toBase64(bytes) {
  let text = "";
  for (let i = 0; i < bytes.length; i++) text += String.fromCharCode(bytes[i]);
  return btoa(text);
}

// transfer_client.js counts milliseconds; the page's Console counts seconds.
const inMilliseconds = (console) => ({command: (text, ms) => console.command(text, ms / 1000)});

export async function publish(console, update, progress) {
  if (console.device && console.binaryTransfer !== false) {
    try {
      await upload(inMilliseconds(console), linkFromHid(console.device), update, progress);
      return;
    } catch (error) {
      if (!(error instanceof TransferUnavailable)) throw error;
      // Not advertised, or refused before any data moved: use the Base64 console path.
    }
  }
  let total = 0;
  for (const block of update.segments.values()) total += block.length;
  let processed = 0, lastPercent = 0;
  progress("Writing dictionary data… 0%");
  for (const [segment, block] of update.segments) {
    await updateCommand(console, `erase ${segment}`);
    for (let offset = 0; offset < block.length; offset += 512) {
      const data = block.subarray(offset, offset + 512);
      if (!data.every((byte) => byte === 0xff)) await updateCommand(console, `write ${segment * SEGMENT + offset} ${toBase64(data)}`);
      processed += data.length;
      const percent = Math.floor(processed * 100 / total);
      if (percent >= lastPercent + 5 || percent === 100) {
        progress(`Writing dictionary data… ${percent}%`);
        lastPercent = percent;
      }
    }
  }
  progress("Verifying staged data and saving the catalog…");
  const reply = await updateCommand(console, `commit ${update.metadata} ${update.payload.length} ${crc32(update.payload)}`, 1800);
  if (reply !== "OK Rebooting") throw runtimeError("Unexpected commit response");
}

export async function abort(console) {
  try { await updateCommand(console, "abort", 2); } catch { /* the keyboard drops an abandoned session by itself */ }
}

function u32(value) {
  const out = new Uint8Array(4);
  new DataView(out.buffer).setUint32(0, value, true);
  return out;
}

// What a journal-cleanup preview reports, and the token the later apply must present unchanged.
async function compactToken(console, answer, snapshot, update) {
  const view = new DataView(update.payload.buffer, update.payload.byteOffset, update.payload.byteLength);
  const rows = view.getUint16(4, true);
  const nextJournals = new Set();
  for (let at = 32 + 96 * rows; at + 24 <= update.payload.length; at += 24) {
    if (view.getUint32(at + 12, true) === 2) nextJournals.add(view.getUint16(at, true));
  }
  const victims = snapshot.journals.map((extent) => extent[0]).filter((segment) => !nextJournals.has(segment)).sort((a, b) => a - b);
  const highwater = new Uint8Array(8);
  new DataView(highwater.buffer).setBigUint64(0, snapshot.highwater, true);
  const parts = [utf8(console.serial + answer), snapshot.payload, highwater];
  for (const [segment, block] of [...update.segments].sort((a, b) => a[0] - b[0])) parts.push(u32(segment), u32(block.length), block);
  return {token: await sha256Hex(...parts), victims};
}

// Preflight, upload and commit one update over the console. Resolves true when something was published.
export async function transfer(console, incoming = null, progress = () => {}, {move = null, rename = null, delete: remove = null,
    previewCompact = false, expectedCompact = null} = {}) {
  const command = (text, timeout = 30) => updateCommand(console, text, timeout);
  const mode = move ? "reorder" : rename ? "rename" : remove ? "delete" : incoming === null ? "compact" : "import";
  if ((previewCompact || expectedCompact !== null) && mode !== "compact") throw new Error("A journal preview only applies to compaction");
  const answer = await command("begin " + mode);
  try {
    progress("Reading the current dictionary store and planning free space…");
    const {snapshot, protected: protectedSegments} = await readSnapshot(console, answer, {metadataOnly: Boolean(move || rename || remove)});
    const update = plan(snapshot, protectedSegments, incoming, {move, rename, delete: remove});
    if (previewCompact || expectedCompact !== null) {
      let token = null, victims = [];
      if (update !== null) ({token, victims} = await compactToken(console, answer, snapshot, update));
      if (previewCompact) {
        await command("abort");
        return {token, reclaimable: victims};
      }
      if (token !== expectedCompact) throw new Error("Journal contents or allocation changed. Preview cleanup again.");
    }
    if (update === null) {
      await command("abort");
      if (move) progress("The dictionary is already at that priority.");
      else if (rename) progress("The dictionary already has that name.");
      else if (incoming === null) progress("No journals can be reclaimed in this pass.");
      else progress("All imported entries already match.");
      return false;
    }
    await publish(console, update, progress);
    if (move) progress(`Moved ${move[0]} to priority ${move[1]}; the left half is rebooting.`);
    else if (rename) progress(`Renamed ${rename[0]} to ${rename[1]}; the left half is rebooting.`);
    else if (remove) progress(`Deleted ${remove}; the left half is rebooting.`);
    else if (incoming !== null) progress(`Published ${update.changes.toLocaleString("en-US")} entries; the left half is rebooting.`);
    else progress(`Reclaimed ${update.reclaimed} old journals; the left half is rebooting.`);
    return true;
  } catch (error) {
    await abort(console);
    throw error;
  }
}
