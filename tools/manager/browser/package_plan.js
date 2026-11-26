// Manager milestone M2: install or uninstall a factory dictionary, ported from plan_package in
// tools/storage/native_base_update.py. `compiled` is {contents, pointers, digest} (the pinned compiled image and the SHA-256
// of its source). Resolves to {update, report} with the same bytes and the same report (and confirmation hash) as Python;
// golden vectors from golden_package.py gate it.
import {NoSpace} from "./plan.js";
import {SEGMENT, rowName} from "./snapshot.js";
import {CompactImage, stageAllocation, inspectFree, packExtent, unpackExtent, sha256Hex} from "./snapshot_plan.js";

// package key -> [identity, name], from build_native_image.FACTORY_KEYS.
export const FACTORY_KEYS = {
  "lapwing-commands": [2, "Lapwing Commands"], "lapwing-numbers": [3, "Lapwing Numbers"], "lapwing-uk": [4, "Lapwing UK Additions"],
  "lapwing-proper": [5, "Lapwing Proper Nouns"], "lapwing-base": [6, "Lapwing Base"], "plover": [7, "Plover Base"],
};
const FACTORY_ABSENT = 2;
const view = (bytes) => new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
const concat = (...parts) => {
  const out = new Uint8Array(parts.reduce((sum, part) => sum + part.length, 0));
  let at = 0;
  for (const part of parts) { out.set(part, at); at += part.length; }
  return out;
};
const sortedNumbers = (set) => [...set].sort((a, b) => a - b);

export async function planPackage(snapshot, protectedSegments, pkg, restore = false, compiled = null) {
  const [identity, name] = FACTORY_KEYS[pkg];
  const rows = snapshot.rows.map((row) => Uint8Array.from(row));
  const row = rows.find((candidate) => view(candidate).getUint32(0, true) === identity) ?? null;
  if (row === null || rowName(row) !== name) throw new Error("Pinned dictionary row is missing");
  const extents = snapshot.extents.map((extent) => [...extent]);
  const {blocks, extents: added, write} = stageAllocation(protectedSegments, snapshot.poolEnd);
  let packageSegments;
  if (restore) {
    if (row[11] !== FACTORY_ABSENT) throw new Error("The factory dictionary is already installed");
    if (!compiled) throw new Error("Verified pinned sources are required to install the dictionary");
    if (extents.length === 253) throw new NoSpace("Catalog extent limit");
    const base = extents.length;
    write(new CompactImage(compiled.contents, compiled.pointers), 1);
    for (const extent of added) extents.push(unpackExtent(extent));
    view(row).setUint16(8, base, true); row[10] = 0; row[11] = 0;
    row.set(compiled.digest, 64);
    packageSegments = extents[base][1];
  } else {
    if (row[11] === FACTORY_ABSENT) throw new Error("The factory dictionary is already uninstalled");
    const base = view(row).getUint16(8, true), member = row[10];
    if (base >= extents.length || member || row[11]) throw new Error("The factory dictionary requires an individual flash image");
    view(row).setUint32(4, 0, true); view(row).setUint16(8, 65535, true); row[10] = 0; row[11] = FACTORY_ABSENT;
    for (const other of rows) {
      const reference = view(other).getUint16(8, true);
      if (reference === base) throw new Error("The factory dictionary requires an individual flash image");
      if (base < reference && reference < 65535) view(other).setUint16(8, reference - 1, true);
    }
    packageSegments = extents.splice(base, 1)[0][1];
  }
  const prefix = snapshot.payload.slice(0, 32);
  view(prefix).setUint16(6, extents.length, true);
  const payload = concat(prefix, ...rows, ...extents.map((extent) => packExtent(...extent)));
  const metadata = write(payload, 0);
  const current = new Set([0, 1, snapshot.poolEnd]);
  for (const [start, count] of snapshot.extents) for (let s = start; s < start + count; s++) current.add(s);
  const target = new Set([0, 1, snapshot.poolEnd]);
  for (const [start, count] of extents) for (let s = start; s < start + count; s++) target.add(s);
  const owned = sortedNumbers(protectedSegments);
  const owners = new Uint8Array(owned.length * 2);
  owned.forEach((segment, i) => view(owners).setUint16(i * 2, segment, true));
  const confirmation = await sha256Hex(snapshot.payload, owners, payload, ...sortedNumbers(blocks.keys()).map((segment) => blocks.get(segment)));
  const [free, run] = inspectFree(protectedSegments, snapshot.poolEnd);
  const [after, afterRun] = inspectFree(target, snapshot.poolEnd);
  const report = {
    package: pkg, name, dictionary_id: identity, restoring: restore, package_segments: packageSegments, staging_segments: blocks.size,
    free_now: free, largest_run_now: run, free_after_one_root: inspectFree(new Set([...current, ...target]), snapshot.poolEnd)[0],
    free_after_both_roots: after, largest_run_after_both_roots: afterRun, package_segments_released: restore ? 0 : packageSegments,
    retained_only_segments: [...protectedSegments].filter((segment) => !current.has(segment)).length, confirmation,
  };
  return {update: {segments: blocks, metadata, payload, reclaimed: 0, changes: 0}, report};
}

// The approval the Manager asks the user to confirm: the plan's hash bound to the keyboard's serial number.
export const packageApproval = (serial, confirmation) => sha256Hex(new TextEncoder().encode(`${serial}:${confirmation}`));
