// Manager: a complete backup read from the keyboard, ported from tools/storage/native_update.py (backup_all). Freezes a view,
// captures the catalog and every referenced extent through the console, checks the host layout did not change meanwhile, stamps
// the keyboard's serial and catalog generation, and always aborts the view.
import {updateCommand, readFlash, readSnapshot} from "./device_reads.js";
import {capture, digest} from "./recovery.js";
import {PY_SPACE} from "./console.js";
import {runtimeError} from "./errors.js";

// datetime.now(timezone.utc).isoformat(): microseconds only when they are not zero.
export function isoNow(now = new Date()) {
  const micros = String(now.getUTCMilliseconds() * 1000).padStart(6, "0");
  return now.toISOString().slice(0, 19) + (micros === "000000" ? "" : "." + micros) + "+00:00";
}

// `now()` supplies the capture timestamp (a test passes a fixed one).
export async function backupAll(console, {onProgress = null, now = isoNow} = {}) {
  const layout = await console.command("get_parameter host_layout");
  const answer = await updateCommand(console, "begin backup");
  try {
    const {snapshot} = await readSnapshot(console, answer, {metadataOnly: true});
    const total = snapshot.extents.reduce((sum, extent) => sum + extent[2], 0);
    const reporting = onProgress !== null && total > 0;
    let received = 0, lastPercent = 0;
    if (reporting) onProgress(0, total);
    const countBytes = (count) => {
      received += count;
      const percent = Math.floor(received * 100 / total);
      if (percent !== lastPercent) {
        lastPercent = percent;
        onProgress(received, total);
      }
    };
    const result = await capture((start, size) => readFlash(console, start, size, reporting ? countBytes : null), snapshot, layout);
    if (await console.command("get_parameter host_layout") !== layout) throw runtimeError("Host layout changed during backup. Retry the backup.");
    const serial = console.serial;
    if (serial) {
      const status = (await console.command("native_install status")).split(PY_SPACE).filter((part) => part !== "");
      if (status.length !== 3 || status[0] !== "NATIVE2" || status[1] !== "READY" || !/^[0-9]+$/.test(status[2])) {
        throw runtimeError("Could not read the captured catalog generation.");
      }
      result.capture = {serial, generation: Number(status[2]), timestamp: now()};
      result.sha256 = await digest(result);
    }
    return result;
  } finally {
    await updateCommand(console, "abort", 5);
  }
}
