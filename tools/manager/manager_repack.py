"""Keep verified repack files on the computer and use the guarded USB installer."""

import hashlib
import json
from pathlib import Path
import re
import secrets

from build_native_image import SEGMENT, overlay_record_span
from native_layout import read_images, read_layout
from native_recovery import reconstruct, recover_usb, save_atomic, sync_directory
from native_repack import RESERVE, build_merge_repack, build_repack, read_bundle, restore_repack, segment_count
from native_snapshot import offline_merge_token
from native_update import backup_all, overlay_usage


def backup_directory(serial):
    if not serial:
        raise ValueError("Connect a keyboard with an identifiable USB serial.")
    identity = hashlib.sha256(serial.encode()).hexdigest()
    return Path.home() / ".stenosquid" / "repack" / identity


def plan_directory(serial, token):
    if not isinstance(token, str) or not re.fullmatch(r"[0-9a-f]{32}", token):
        raise ValueError("Preview a repack for this keyboard first.")
    return backup_directory(serial) / token


def project_layout(images, usage):
    """Place the existing compact images and a conservative journal estimate."""
    kinds = ["c", "c"] + ["."] * 253 + ["x"]
    next_block = 2
    projected = []

    def place(kind, blocks):
        nonlocal next_block
        start = next_block
        next_block += blocks
        if next_block > 255 - RESERVE:
            raise ValueError("The projected repack could use the maintenance reserve.")
        kinds[start:next_block] = [kind] * blocks
        return start

    journal_placed = False
    order = sorted(images, key=lambda image: (image["id"] if image["kind"] == "b" else 8, image["extent"]))
    for image in order:
        if image["kind"] == "s" and not journal_placed:
            place("j", 1)
            journal_placed = True
        projected.append({**image, "start": place(image["kind"], image["segments"])})
    if not journal_placed:
        place("j", 1)
    live_bytes = sum(row["bytes"] for row in usage.values())
    # The actual builder packs unsplit records in outline order. Each closed
    # block holds at least one block's payload minus the largest possible gap.
    # Count the upper bound so the preview never promises more free space.
    payload = SEGMENT - 256
    largest_record = overlay_record_span((1,) * 16, b"x" * 2047)
    guaranteed = payload - largest_record + 1
    extra = max(0, live_bytes - payload)
    place("j", (extra + guaranteed - 1) // guaranteed)
    place("f", segment_count(4 * sum(row["count"] for row in usage.values())))
    place("r", segment_count(4 * sum(row["entries"] for row in usage.values())))
    return {"segments": [{"index": index, "kind": kind} for index, kind in enumerate(kinds)],
            "images": projected, "projected": True, "free_segments": 255 - next_block,
            "largest_free_run": 255 - next_block}


def preview_repack(console, progress):
    if (console.command("native_install update repack-capabilities") != "OK REPACK1" or
            console.command("native_install update recovery-capabilities") != "OK RECOVERY1"):
        raise RuntimeError("This keyboard does not offer guarded USB repacking.")
    progress("Reading the flash layout and journal metadata…")
    keys = ("generation", "tail")
    start = json.loads(console.command("user_dictionary_status"))
    state = {key: start[key] for key in keys}
    before = read_layout(console)
    if before["staging_blocked"] or before["free_segments"] is None:
        raise RuntimeError("The flash layout is busy or unavailable. Scan and preview again.")
    images = read_images(console)
    usage = overlay_usage(console, metadata_only=True)
    end = json.loads(console.command("user_dictionary_status"))
    if (any(end[key] != state[key] for key in keys) or
            console.command("native_install status") != f"NATIVE2 READY {state['generation']}"):
        raise RuntimeError("Flash changed during the preview. Scan and preview again.")
    after = project_layout(images, usage)
    keys = ("largest_free_run", "free_segments")
    result = {"serial": console.serial, "state": state,
              "before": {key: before[key] for key in keys}, "after": after}
    if tuple(after[key] for key in keys) > tuple(before[key] for key in keys):
        approval = json.dumps([result, before["segments"], usage], sort_keys=True).encode()
        result["token"] = hashlib.sha256(approval).hexdigest()[:32]
    return result


def capture_approved_repack(console, request, progress):
    serial = console.serial
    if request.get("serial") != serial or request.get("confirm") != serial:
        raise ValueError("Confirm the USB serial before rewriting the dictionary flash.")
    plan = preview_repack(console, progress)
    if not plan.get("token") or request.get("token") != plan["token"]:
        raise ValueError("Flash changed since the approved preview. Preview USB repack again.")

    progress("Backing up dictionaries and saved edits…")
    source = backup_all(console)
    status = json.loads(console.command("user_dictionary_status"))
    if (source["capture"]["generation"] != plan["state"]["generation"] or
            any(status[key] != value for key, value in plan["state"].items())):
        raise RuntimeError("Flash changed during backup. No erase was started. Preview USB repack again.")
    return source, plan


def install_repack(source, plan, progress):
    serial = plan["serial"]
    if source.get("capture", {}).get("serial") != serial:
        raise ValueError("The saved backup belongs to a different keyboard.")
    progress("Building and verifying the packed layout on this computer…")
    _, target, report = build_repack(source, [])
    after = report["target_layout"]
    if any(after[key] < plan["after"][key] for key in ("free_segments", "largest_free_run")):
        raise RuntimeError("The verified plan has less space than the approved preview. No erase was started.")
    install_verified_bundle(source, target, serial, progress)


def capture_approved_merge(console, request, progress):
    serial = console.serial
    upper, lower = request.get("upper"), request.get("lower")
    token = request.get("token")
    if (not serial or request.get("serial") != serial or request.get("confirm") != serial
            or not isinstance(token, str) or not re.fullmatch(r"[0-9a-f]{64}", token)
            or any(type(identity) is not int or not 0 < identity <= 0xffffffff for identity in (upper, lower))):
        raise ValueError("Confirm a USB merge preview for this keyboard.")
    if (console.command("native_install update repack-capabilities") != "OK REPACK1"
            or console.command("native_install update recovery-capabilities") != "OK RECOVERY1"):
        raise RuntimeError("This keyboard does not offer guarded USB repacking.")

    progress("Backing up dictionaries and saved edits…")
    source = backup_all(console)
    original = reconstruct(source)
    if token != offline_merge_token(original[1], serial, upper, lower):
        raise ValueError("The dictionaries changed after the preview. No erase was started. Preview the merge again.")
    return source, original


def install_offline_merge(source, original, request, progress):
    progress("Building and verifying the merged layout on this computer…")
    target, name = build_merge_repack(source, original, request["upper"], request["lower"])
    install_verified_bundle(source, target, request["serial"], progress)
    return name


def install_verified_bundle(source, target, serial, progress):
    directory = plan_directory(serial, secrets.token_hex(16))
    directory.mkdir(mode=0o700, parents=True)
    # Make the backup discoverable before any operation can erase the pool.
    for parent in directory.parents:
        sync_directory(parent)
        if parent == Path.home():
            break
    original, optimized = directory / "original.json", directory / "optimized.json"
    save_atomic(original, source)
    save_atomic(optimized, target)
    _, target_hash = read_bundle(optimized, durable=True)
    save_atomic(directory / "attempt.json", {"original_sha256": source["sha256"],
                                            "optimized_file_sha256": target_hash})
    restore_repack(original, optimized, serial, f"{serial}:{target_hash}", progress, live=True)


def last_repack_backup(serial):
    directory = backup_directory(serial)
    markers = [path for path in directory.glob("*/attempt.json") if (path.parent / "original.json").is_file()]
    if not markers:
        return None
    return max(markers, key=lambda path: path.stat().st_mtime_ns).parent


def restore_original(serial, request, progress):
    if request.get("serial") != serial or request.get("confirm") != serial:
        raise ValueError("Confirm the USB serial before restoring the original backup.")
    directory = plan_directory(serial, request.get("token"))
    if directory != last_repack_backup(serial):
        raise ValueError("Refresh the keyboard to select its latest repack backup.")
    source, _ = read_bundle(directory / "original.json", durable=True)
    marker, _ = read_bundle(directory / "attempt.json", durable=True)
    if (source.get("capture", {}).get("serial") != serial or
            marker.get("original_sha256") != source["sha256"]):
        raise ValueError("The original repack backup changed or belongs to another keyboard.")
    recover_usb(source, serial, serial, progress, unprovisioned_only=True)
