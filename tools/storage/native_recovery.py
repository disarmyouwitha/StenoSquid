"""Self-contained experimental backups and offline reconstruction.

The bundle holds the selected catalog and its exact referenced extents. Keeping
their physical addresses preserves mapped pointers and both journal indexes.
Offline verification is the default. USB recovery is an experimental opt-in.
"""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import struct
import tempfile
import zlib

from build_native_image import LIMIT, SEGMENT, SOURCES, XIP, catalog_header
from hybrid import compact_entries
from jsc4 import reader, require_snapshot_hint
from native_update import EXTENT, Snapshot, row_name, validate_name

FORMAT = "stenosquid-backup-2"
LAYOUTS = ("Mac U.S.", "Windows U.S. (plain text)")


def digest(backup):
    manifest = {key: value for key, value in backup.items() if key != "sha256"}
    data = json.dumps(manifest, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(data.encode("ascii")).hexdigest()


def pack(data):
    return base64.b64encode(data).decode("ascii")


def unpack(encoded, expected=None):
    if not isinstance(encoded, str) or len(encoded) > (LIMIT + 2) // 3 * 4:
        raise ValueError("Invalid recovery data size")
    data = base64.b64decode(encoded, validate=True)
    if expected is not None and len(data) != expected:
        raise ValueError("Recovery data size differs from its catalog")
    return data


def descriptors(payload):
    if not 32 <= len(payload) <= 32 + 64 * 96 + 253 * 24:
        raise ValueError("Invalid recovery catalog size")
    next_id, rows, count = struct.unpack_from("<IHH", payload)
    end = 32 + rows * 96
    if next_id < 8 or rows > 64 or count > 253 or any(payload[16:32]) or len(payload) != end + count * 24:
        raise ValueError("Invalid recovery catalog shape")
    used, kinds = {0, 1, 255}, set()
    result = list(EXTENT.iter_unpack(payload[end:]))
    for start, span, size, crc, kind, epoch in result:
        run = set(range(start, start + span))
        if (not 2 <= start < 255 or not 1 <= span <= 255 - start
                or not 0 < size <= span * SEGMENT or run & used or kind not in (1, 2, 3, 4)
                or (kind == 2 and (span != 1 or size != SEGMENT or not epoch or crc))
                or (kind != 2 and epoch) or (kind in (3, 4) and (size % 4 or kind in kinds))):
            raise ValueError("Invalid or overlapping recovery extent")
        used.update(run)
        kinds.add(kind)
    if 2 not in kinds:
        raise ValueError("Recovery catalog is missing its journal")
    return result


def capture(console_read, snapshot, layout):
    payload = snapshot.payload
    backup = dict(format=FORMAT, host_layout=layout, catalog=pack(payload), extents=[
        pack(console_read(start * SEGMENT, size)) for start, _, size, _, _, _ in descriptors(payload)
    ])
    backup["sha256"] = digest(backup)
    reconstruct(backup)
    return backup


def validate_rows(snapshot, image):
    factory = {identity: name for identity, name, _, source in SOURCES if source}
    references, names, ids = {}, set(), set()
    counts = {}
    for row in snapshot.rows:
        identity, enabled, base, member, marker, name_bytes = struct.unpack_from("<IIHBBB", row)
        name = row_name(row)
        if (not 0 < identity < snapshot.next_id or identity in ids or name in names
                or enabled > 1 or not 1 <= name_bytes <= 44 or any(row[13:16])
                or any(row[16 + name_bytes:64]) or marker > 2):
            raise ValueError("Invalid recovery dictionary row")
        names.add(name)
        ids.add(identity)
        if identity in factory:
            if name != factory[identity] or not any(row[64:96]):
                raise ValueError("Invalid pinned source identity")
            absent = marker == 2 and base == 65535 and not enabled and not member
            if not absent and (marker or base == 65535):
                raise ValueError("Invalid factory image state")
        else:
            if name != "user_dictionary":
                if identity < 8 or name in factory.values():
                    raise ValueError("Invalid user-owned dictionary identity")
                validate_name(name)
            if marker > 1 or any(row[64:96]) or bool(marker) != (base != 65535) or member:
                raise ValueError("Invalid user-owned snapshot descriptor")
        if base == 65535:
            if member:
                raise ValueError("Unexpected compiled member for a journal dictionary")
            continue
        if base >= len(snapshot.extents) or snapshot.extents[base][4] != 1:
            raise ValueError("Invalid compact image reference")
        members = references.setdefault(base, set())
        if member in members:
            raise ValueError("Duplicate compiled image member")
        members.add(member)
        start, _, size, _, _, _ = snapshot.extents[base]
        address = start * SEGMENT
        blob = image[address:address + size]
        if len(blob) < 40:
            raise ValueError("Truncated compact image")
        count = struct.unpack_from("<H", blob, 4)[0]
        if count != 1 or member:
            raise ValueError("Invalid compact image member count")
        counts[base] = count
        if marker:
            require_snapshot_hint(blob, XIP + address)
        # Walk the compiled data using the same host reader as export.
        try:
            lookup = reader(blob, XIP + address, member, count)
            seen = set()
            for outline, text in compact_entries(blob, XIP + address, member, count):
                if outline in seen or lookup(outline) != text:
                    raise ValueError("Compact recovery image failed lookup verification")
                seen.add(outline)
        except (AssertionError, IndexError, struct.error) as error:
            raise ValueError("Malformed compact recovery image") from error
    if not (factory.keys() <= ids and "user_dictionary" in names):
        raise ValueError("Recovery bundle is missing a pinned or personal dictionary")
    if any(members != set(range(counts[base])) for base, members in references.items()):
        raise ValueError("Recovery bundle is missing a compact image member")
    if set(references) != {i for i, extent in enumerate(snapshot.extents) if extent[4] == 1}:
        raise ValueError("Unreferenced compact extent in recovery bundle")


def reconstruct(backup):
    """Return a pair of coherent roots for offline validation only."""
    if (not isinstance(backup, dict) or backup.get("format") != FORMAT
            or backup.get("host_layout") not in LAYOUTS or backup.get("sha256") != digest(backup)):
        raise ValueError("Recovery bundle version, host layout or checksum is invalid")
    if "capture" in backup:
        source = backup["capture"]
        if (not isinstance(source, dict) or not isinstance(source.get("serial"), str) or not source["serial"]
                or type(source.get("generation")) is not int or source["generation"] < 1
                or not isinstance(source.get("timestamp"), str) or not source["timestamp"]):
            raise ValueError("Invalid recovery capture identity")
    payload = unpack(backup.get("catalog"))
    extents = descriptors(payload)
    chunks = backup.get("extents")
    if not isinstance(chunks, list) or len(chunks) != len(extents):
        raise ValueError("Recovery bundle is missing an extent")
    image = bytearray(b"\xff" * LIMIT)
    for (start, _, size, crc, kind, _), data in zip(extents, chunks):
        blob = unpack(data, size)
        if kind != 2 and zlib.crc32(blob) != crc:
            raise ValueError("Recovery extent checksum differs from its catalog")
        address = start * SEGMENT
        image[address:address + size] = blob
    for slot in range(2):
        start = slot * SEGMENT
        image[start:start + 32 + len(payload)] = catalog_header(slot + 1, payload) + payload

    def read(address, size):
        return bytes(image[address:address + size])

    snapshot = Snapshot(read, 32, len(payload), zlib.crc32(payload), strict=True)
    validate_rows(snapshot, image)
    return bytes(image), snapshot


def sync_directory(path):
    """Flush the directory entry where directory file descriptors are supported."""
    if os.name != "posix":
        return
    directory = os.open(path, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def save_atomic(path, document, dump=json.dump):
    """Publish only a flushed, complete file and refuse to replace any backup."""
    path = Path(path)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as output:
        temporary = Path(output.name)
        try:
            dump(document, output, ensure_ascii=False, separators=(",", ":"))
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
            os.link(temporary, path)
            sync_directory(path.parent)
        finally:
            temporary.unlink(missing_ok=True)


def recovery_images(image):
    staged = bytearray(image)
    for root in (0, SEGMENT):
        staged[root + 28:root + 32] = b"\xff" * 4
    return bytes(staged), zlib.crc32(staged), zlib.crc32(image)


def recover_usb(backup, serial, confirmation, progress=print, *, repack_spare=False, live_source=None,
                unprovisioned_only=False):
    if not serial or confirmation != serial:
        raise ValueError("Recovery needs an explicit USB serial and identical erase confirmation.")
    if ("repack" in backup) != (repack_spare or live_source is not None) or (repack_spare and live_source is not None):
        raise ValueError("Optimized bundles require the repack workflow with both backup files.")
    image, snapshot = reconstruct(backup)
    staged, staged_crc, final_crc = recovery_images(image)
    from native_install import FACTORY_CAPABILITIES, command, open_console, wait_ready, write_pool
    from native_manage import checked

    def matches(console):
        result = console.command(f"native_install match RECOVERY {final_crc}", timeout=180)
        if result not in ("OK MATCH", "ERR Recovery contents differ"):
            raise RuntimeError(f"Full-pool verification unavailable: {result}. No erase was retried.")
        return result == "OK MATCH"

    with open_console(serial) as console:
        if console.serial != serial:
            raise RuntimeError("The connected USB serial differs from the confirmed recovery target.")
        if console.command("native_install update recovery-capabilities") != "OK RECOVERY1":
            raise RuntimeError("Flash firmware with Native USB recovery support first.")
        if any(row[11] == 2 for row in snapshot.rows) and console.command(FACTORY_CAPABILITIES) != "OK FACTORY2":
            raise RuntimeError("Update the target firmware before recovering uninstalled factory dictionaries.")
        status = console.command("native_install status")
        state = status.split()[:2]
        if state not in (["NATIVE2", "READY"], ["NATIVE2", "UNPROVISIONED"]):
            raise RuntimeError("Native recovery transport is unavailable or busy. No erase was started.")
        if unprovisioned_only and state[1] != "UNPROVISIONED":
            raise RuntimeError("The keyboard already has a readable dictionary pool. No erase was started.")
        already_installed = state[1] == "READY" and matches(console)
        if not already_installed:
            if repack_spare and state[1] != "UNPROVISIONED":
                raise RuntimeError("Optimized recovery requires an unprovisioned expendable spare. "
                                   "A live or busy pool cannot be replaced.")
            if live_source is None:
                begin = f"begin RECOVERY {staged_crc} {final_crc}"
            else:
                source, view = live_source
                if state[1] != "READY" or console.command(
                        "native_install update repack-capabilities") != "OK REPACK1":
                    raise RuntimeError("The live pool must be READY and running firmware with guarded repack support.")
                from native_repack import check_live

                progress("Comparing the live catalog and all referenced bytes with the original backup…")
                check_live(console, source)
                generation = source["capture"]["generation"]
                sequence = view.highwater
                layout = LAYOUTS.index(source["host_layout"])
                begin = (f"begin REPACK {staged_crc} {final_crc} "
                         f"{generation & 0xffffffff} {generation >> 32} {zlib.crc32(view.payload)} "
                         f"{sequence & 0xffffffff} {sequence >> 32} {layout}")
            command(console, begin)
            try:
                write_pool(console, staged, progress, finish=False)
            except BaseException:
                try:
                    command(console, "abort", timeout=2)
                except (OSError, RuntimeError, TimeoutError):
                    pass
                raise
            progress("Verifying the staged image before publishing either catalog root…")
            try:
                command(console, "finish", timeout=240)
            except (OSError, RuntimeError, TimeoutError):
                progress("Publication was not acknowledged. Reconnecting to verify the full pool…")
        else:
            progress("The confirmed keyboard already matches this recovery image.")

    if not already_installed:
        try:
            wait_ready(serial)
        except RuntimeError as exc:
            raise RuntimeError("Recovery did not reach READY. Reconnect and repeat explicitly with the same bundle.") from exc
    with open_console(serial) as console:
        if console.serial != serial:
            raise RuntimeError("The recovered USB serial differs from the confirmed target.")
        if not matches(console):
            raise RuntimeError("Publication could not be confirmed by full-pool CRC. "
                               "Nothing was erased again. Inspect the board before retrying.")
        actual = json.loads(console.command("native_catalog"))
        wanted = [(struct.unpack_from("<I", row)[0], row_name(row), bool(row[4])) for row in snapshot.rows]
        if [(row["id"], row["name"], row["enabled"]) for row in actual] != wanted:
            raise RuntimeError("The recovered catalog does not match the bundle.")
        checked(console, "set_host_layout " + backup["host_layout"])
        if console.command("get_parameter host_layout") != backup["host_layout"]:
            raise RuntimeError("The dictionaries recovered, but the host layout could not be restored.")
    progress("Recovery complete. Full-pool CRC, dictionary catalog and host layout match the bundle.")


def main():
    parser = argparse.ArgumentParser(description="Verify a complete experimental backup offline.")
    parser.add_argument("backup", type=Path)
    parser.add_argument("--restore", action="store_true", help="Run explicit experimental recovery on a spare")
    parser.add_argument("--serial", help="Exact USB serial on the spare keyboard")
    parser.add_argument("--confirm-erase", metavar="USB_SERIAL", help="Repeat the target serial to authorize erasing the shared pool")
    args = parser.parse_args()
    if args.restore and (not args.serial or args.confirm_erase != args.serial):
        parser.error("--restore requires --serial and --confirm-erase with the exact same USB serial")
    if not args.restore and (args.serial or args.confirm_erase):
        parser.error("USB options require --restore")
    backup = json.loads(args.backup.read_text(encoding="utf-8"))
    if args.restore:
        recover_usb(backup, args.serial, args.confirm_erase)
        return
    _, snapshot = reconstruct(backup)
    print(f"Verified {len(snapshot.rows)} dictionaries, {len(snapshot.entries):,} live overlay records"
          f" and {len(snapshot.extents)} extents. Host layout: {backup['host_layout']}.")


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, ValueError) as error:
        raise SystemExit(f"Error: {error}") from error
