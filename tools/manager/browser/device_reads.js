// Manager milestone M1 (second half): reading a frozen catalog snapshot through the keyboard's console, ported from
// tools/storage/native_update.py (update_command, read_flash, read_live_journal, read_snapshot). `console` is any
// object with `command(text, timeoutSeconds) -> Promise<string>` (the WebHID console in the Manager, a fake in tests).
import {runtimeError} from "./errors.js";
import {Snapshot, SEGMENT, LARGE_POOL_END, SMALL_POOL_END, COMMIT} from "./snapshot.js";

export async function updateCommand(console, text, timeout = 30) {
  const reply = await console.command("native_install update " + text, timeout);
  if (reply !== "OK" && !reply.startsWith("OK ")) throw runtimeError(reply);
  return reply;
}

function decodeBase64(text) {
  // Python used b64decode(..., validate=True): only the base64 alphabet, correctly padded.
  if (!/^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/.test(text)) throw new Error("Invalid base64 in a flash read");
  return Uint8Array.from(atob(text), (c) => c.charCodeAt(0));
}

export async function readFlash(console, start, size, onRead = null) {
  const result = new Uint8Array(size);
  let filled = 0;
  for (let offset = 0; offset < size; offset += 512) {
    const count = Math.min(size - offset, 512);
    const reply = await updateCommand(console, `read ${start + offset} ${count}`);
    const data = decodeBase64(reply.slice(3));
    if (data.length !== count) throw runtimeError("Short snapshot read");
    result.set(data, filled);
    filled += count;
    if (onRead) onRead(count);
  }
  return result;
}

// Reads only through the first unpublished record of a frozen journal and pads the rest with 0xFF
// (for logical editing only; exact recovery backups always use full reads).
export async function readLiveJournal(console, start, onRead = null) {
  const data = new Uint8Array(SEGMENT).fill(0xff);
  let have = 0;
  const ensure = async (end) => {
    while (have < end) {
      const size = Math.min(512, SEGMENT - have);
      const chunk = await readFlash(console, start + have, size, onRead);
      data.set(chunk, have);
      have += chunk.length;
    }
  };
  await ensure(512);
  if (String.fromCharCode(...data.subarray(0, 8)) === "SQJSEG01") {
    const view = new DataView(data.buffer);
    let offset = 256;
    while (offset + 32 <= SEGMENT) {
      await ensure(offset + 32);
      if (view.getUint32(offset + 28, true) !== COMMIT) break;
      const span = view.getUint32(offset + 4, true);
      if (span < 44 || span % 4 || offset + span > SEGMENT) break; // Snapshot reports the malformed committed record.
      await ensure(offset + span);
      offset += span;
    }
  }
  return data;
}

// `answer` is the reply to `begin backup` / `begin metadata`: "OK <catalog address> <length> <crc> <ownership bitmap>".
// Resolves to {snapshot, protected: Set of segment numbers}.
export async function readSnapshot(console, answer, {metadataOnly = false, onRead = null, trimJournals = false} = {}) {
  const parts = answer.trim().split(/\s+/);
  if (parts.length !== 5) throw new Error("not enough values to unpack");
  const [, addressText, lengthText, checksumText, ownership] = parts;
  if (ownership.length !== 64 && ownership.length !== 256) throw new Error("Invalid ownership bitmap");
  const words = [];
  for (let i = 0; i < ownership.length; i += 8) {
    const word = ownership.slice(i, i + 8);
    if (!/^[0-9a-fA-F]{8}$/.test(word)) throw new Error("Invalid ownership bitmap");
    words.push(parseInt(word, 16));
  }
  const poolEnd = words.length === 32 ? LARGE_POOL_END : SMALL_POOL_END;
  const protectedSegments = new Set();
  for (let i = 0; i <= poolEnd; i++) if ((words[Math.floor(i / 32)] >>> (i % 32)) & 1) protectedSegments.add(i);
  for (const text of [addressText, lengthText, checksumText]) if (!/^\d+$/.test(text)) throw new Error("invalid literal for int()");
  const address = Number(addressText), length = Number(lengthText), checksum = Number(checksumText);

  let catalog = null, journals = new Set();
  const read = async (start, size) => {
    if (trimJournals && start === address && size === length) {
      catalog ??= await readFlash(console, start, size, onRead);
      return catalog;
    }
    if (trimJournals && journals.has(start) && size === SEGMENT) return readLiveJournal(console, start, onRead);
    return readFlash(console, start, size, onRead);
  };

  if (trimJournals && !metadataOnly) {
    const metadata = await Snapshot.load(read, address, length, checksum, {metadataOnly: true, poolEnd});
    journals = new Set(metadata.extents.filter((extent) => extent[4] === 2).map((extent) => extent[0] * SEGMENT));
  }
  const snapshot = await Snapshot.load(read, address, length, checksum, {metadataOnly, poolEnd});
  return {snapshot, protected: protectedSegments};
}
