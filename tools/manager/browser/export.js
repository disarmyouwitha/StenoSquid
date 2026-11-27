// Manager: reading one dictionary back from the keyboard, ported from tools/storage/native_update.py (export_dictionary).
// Opens a frozen backup view, reads the catalog and the dictionary's compiled base plus journal edits, and always aborts the
// view (even after a failure; a refused abort replaces the original error, as in Python).
import {updateCommand, readFlash, readSnapshot} from "./device_reads.js";
import {effectiveEntries} from "./decode.js";

// Returns a Map(outline -> text). `onSnapshot(snapshot)` is called once the catalog is read.
export async function exportDictionary(console, name, {onRead = null, onSnapshot = null, trimJournals = false} = {}) {
  const answer = await updateCommand(console, "begin backup");
  try {
    const {snapshot} = await readSnapshot(console, answer, {onRead, trimJournals});
    if (onSnapshot !== null) onSnapshot(snapshot);
    return await effectiveEntries(snapshot, name, (at, count) => readFlash(console, at, count, onRead));
  } finally {
    await updateCommand(console, "abort", 5);
  }
}
