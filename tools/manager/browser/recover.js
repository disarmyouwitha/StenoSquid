// Writing a recovery or repack image back to the keyboard over USB, ported from tools/storage/native_recovery.py (recovery_images,
// recover_usb) and tools/storage/native_repack.py (check_live for a source backup, restore_repack). Nothing is erased until the
// bundle has been rebuilt and verified, the keyboard identity matches, and (for a live repack) every byte the original backup
// covers still matches the live pool. After the write the full-pool CRC, the catalog and the host layout are verified again.
import {isRuntimeError, runtimeError} from "./errors.js";
import {PY_SPACE} from "./console.js";
import {backupAll} from "./backup.js";
import {command, beginForPool, readPoolEnd, writePool} from "./install.js";
import {checked} from "./manage.js";
import {LAYOUTS, reconstruct} from "./recovery.js";
import {validatePair} from "./repack.js";
import {SEGMENT, rowName} from "./snapshot.js";
import {FACTORY_CAPABILITIES, parseJson, reportable} from "./state.js";
import {crc32} from "./transfer_frame.js";

const sameJson = (a, b) => JSON.stringify(a) === JSON.stringify(b);

// The image written to the pool (both catalog commit markers erased, so neither root is valid until the final verification) and
// the CRCs of that staged image and of the finished one.
export function recoveryImages(image) {
  const staged = Uint8Array.from(image);
  for (const root of [0, SEGMENT]) staged.fill(0xff, root + 28, root + 32);
  return [staged, crc32(staged), crc32(image)];
}

// Compare every byte covered by the frozen backup with the live pool. This grants no write permission.
export async function checkLive(console, bundle) {
  const actual = await backupAll(console);
  const captured = Object.hasOwn(bundle, "capture") ? bundle.capture : {};
  if (captured && Object.keys(captured).length) {
    for (const key of ["serial", "generation"]) {
      if (captured[key] !== (actual.capture ?? {})[key]) throw runtimeError("Source USB serial or catalog generation changed. Capture a fresh backup and repack.");
    }
  }
  if (["host_layout", "catalog", "extents"].some((key) => !sameJson(actual[key], bundle[key]))) {
    throw runtimeError("Live pool differs from the saved bundle, including journal or layout. For repacking, capture a fresh source backup.");
  }
  return {referenced_bytes_match: true, host_layout: bundle.host_layout};
}

// native_install.wait_ready without a state loader: wait for READY and a dictionary listing after the reboot.
async function waitUntilReady(serial, {opener, clock}) {
  await clock.sleep(3);
  const started = clock.now();
  let deadline = started + 90, sawBusy = false;
  while (clock.now() < deadline) {
    try {
      const {console, close} = await opener(serial);
      try {
        const status = await console.command("native_install status", 2);
        if (status === "ERR Native flash busy" && !sawBusy) { sawBusy = true; deadline = started + 180; }
        if (status.startsWith("NATIVE2 READY ")) {
          if ((await console.command("list_dictionaries", 2)).trimStart().startsWith("[")) return;
        }
      } finally { await close(); }
    } catch (error) {
      if (!reportable(error)) throw error;
    }
    await clock.sleep(1);
  }
  throw runtimeError("The left did not report READY after reboot; image remains available for diagnosis");
}

export async function recoverUsb(backup, serial, confirmation, progress, {repackSpare = false, liveSource = null, unprovisionedOnly = false} = {}, deps) {
  if (!serial || confirmation !== serial) throw new Error("Recovery needs an explicit USB serial and identical erase confirmation.");
  if (Object.hasOwn(backup, "repack") !== (repackSpare || liveSource !== null) || (repackSpare && liveSource !== null)) {
    throw new Error("Optimized bundles require the repack workflow with both backup files.");
  }
  const {image, snapshot} = await reconstruct(backup);
  const [staged, stagedCrc, finalCrc] = recoveryImages(image);

  const matches = async (console) => {
    const result = await console.command(`native_install match RECOVERY ${finalCrc}`, 180);
    if (result !== "OK MATCH" && result !== "ERR Recovery contents differ") throw runtimeError(`Full-pool verification unavailable: ${result}. No erase was retried.`);
    return result === "OK MATCH";
  };

  let alreadyInstalled;
  {
    const {console, close} = await deps.opener(serial);
    try {
      if (console.serial !== serial) throw runtimeError("The connected USB serial differs from the confirmed recovery target.");
      if (await readPoolEnd(console) !== snapshot.poolEnd) throw new Error("The recovery bundle and keyboard have different flash capacities. No erase was started.");
      if (await console.command("native_install update recovery-capabilities", 30) !== "OK RECOVERY1") throw runtimeError("Flash firmware with Native USB recovery support first.");
      if (snapshot.rows.some((row) => row[11] === 2) && await console.command(FACTORY_CAPABILITIES, 30) !== "OK FACTORY2") {
        throw runtimeError("Update the target firmware before recovering uninstalled factory dictionaries.");
      }
      const status = await console.command("native_install status", 30);
      const state = status.split(PY_SPACE).filter((part) => part !== "").slice(0, 2);
      if (!sameJson(state, ["NATIVE2", "READY"]) && !sameJson(state, ["NATIVE2", "UNPROVISIONED"])) throw runtimeError("Native recovery transport is unavailable or busy. No erase was started.");
      if (unprovisionedOnly && state[1] !== "UNPROVISIONED") throw runtimeError("The keyboard already has a readable dictionary pool. No erase was started.");
      alreadyInstalled = state[1] === "READY" && await matches(console);
      if (!alreadyInstalled) {
        if (repackSpare && state[1] !== "UNPROVISIONED") {
          throw runtimeError("Optimized recovery requires an unprovisioned expendable spare. A live or busy pool cannot be replaced.");
        }
        let begin;
        if (liveSource === null) {
          begin = `begin RECOVERY ${stagedCrc} ${finalCrc}`;
        } else {
          const [source, view] = liveSource;
          if (state[1] !== "READY" || await console.command("native_install update repack-capabilities", 30) !== "OK REPACK1") {
            throw runtimeError("The live pool must be READY and running firmware with guarded repack support.");
          }
          progress("Comparing the live catalog and all referenced bytes with the original backup…");
          await checkLive(console, source);
          const generation = BigInt(source.capture.generation), sequence = BigInt(view.highwater);
          const layout = LAYOUTS.indexOf(source.host_layout);
          if (layout < 0) throw new Error(`${source.host_layout} is not in list`);
          begin = `begin REPACK ${stagedCrc} ${finalCrc} ${generation & 0xffffffffn} ${generation >> 32n} ${crc32(view.payload)} ${sequence & 0xffffffffn} ${sequence >> 32n} ${layout}`;
        }
        await command(console, beginForPool(begin, snapshot.poolEnd));
        try {
          await writePool(console, staged, progress, {finish: false});
        } catch (error) {
          try { await command(console, "abort", 2); } catch { /* the original failure is the one to report */ }
          throw error;
        }
        progress("Verifying the staged image before publishing either catalog root…");
        try {
          await command(console, "finish", 240);
        } catch {
          // The keyboard reboots when it publishes, so a missing acknowledgement is expected; the full pool is verified below.
          progress("Publication was not acknowledged. Reconnecting to verify the full pool…");
        }
      } else {
        progress("The confirmed keyboard already matches this recovery image.");
      }
    } finally { await close(); }
  }

  if (!alreadyInstalled) {
    try {
      await waitUntilReady(serial, deps);
    } catch (error) {
      if (!isRuntimeError(error)) throw error;
      throw runtimeError("Recovery did not reach READY. Reconnect and repeat explicitly with the same bundle.");
    }
  }
  const {console, close} = await deps.opener(serial);
  try {
    if (console.serial !== serial) throw runtimeError("The recovered USB serial differs from the confirmed target.");
    if (!await matches(console)) throw runtimeError("Publication could not be confirmed by full-pool CRC. Nothing was erased again. Inspect the board before retrying.");
    const actual = parseJson(await console.command("native_catalog", 30));
    const wanted = snapshot.rows.map((row) => [new DataView(row.buffer, row.byteOffset, row.byteLength).getUint32(0, true), rowName(row), Boolean(row[4])]);
    if (!sameJson(actual.map((row) => [row.id, row.name, row.enabled]), wanted)) throw runtimeError("The recovered catalog does not match the bundle.");
    await checked(console, "set_host_layout " + backup.host_layout);
    if (await console.command("get_parameter host_layout", 30) !== backup.host_layout) throw runtimeError("The dictionaries recovered, but the host layout could not be restored.");
  } finally { await close(); }
  progress("Recovery complete. Full-pool CRC, dictionary catalog and host layout match the bundle.");
}

// native_repack.restore_repack for two bundles that were already read: `source` is the original backup and `target` the optimized one,
// each with the SHA-256 of its file. `confirmation` must be `${serial}:${targetFileHash}`.
export async function restoreRepack(source, target, targetFileHash, serial, confirmation, progress, {live = false, showHashes = true, sourceFileHash = ""} = {}, deps) {
  if (!serial || confirmation !== `${serial}:${targetFileHash}`) throw new Error("Confirm erase with the exact USB serial and optimized file SHA-256.");
  const original = await reconstruct(source);
  await validatePair(source, target, original, await reconstruct(target));
  const capturedSerial = (source.capture ?? {}).serial;
  if (live && capturedSerial !== serial) throw new Error("Live repack requires a fresh backup from the selected keyboard.");
  if (!live && capturedSerial === serial) throw new Error("Use guarded live repack for the capture device.");
  if (showHashes) {
    progress(`Confirmed device ${serial}. Original file SHA-256 ${sourceFileHash}. Optimized file SHA-256 ${targetFileHash}. Recovery erases the entire external dictionary pool.`);
  } else {
    progress("Backup verified. Preparing USB restore…");
  }
  if (live) await recoverUsb(target, serial, serial, progress, {liveSource: [source, original.snapshot]}, deps);
  else await recoverUsb(target, serial, serial, progress, {repackSpare: true}, deps);
}
