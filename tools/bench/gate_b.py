#!/usr/bin/env python3
"""Capture a read-only Gate B baseline, then exercise lean snapshots on a spare."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import struct
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "tools" / part) for part in ("storage", "dictionaries")]
from import_personal import open_console, parse_named
from native_install import wait_ready
from native_layout import read_layout
from native_manage import checked, import_entries
from native_recovery import FORMAT, reconstruct, save_atomic
from native_snapshot import change_snapshot
from native_update import backup_all, effective_entries, export_dictionary, overlay_usage, row_name

FIXTURES = ROOT / "docs/fixtures/adaptive-snapshot"
CHECKPOINT = FIXTURES / "firmware-b9eba3f.json"
PERSONAL = "user_dictionary"
CAPABILITIES = {
    "snapshot-capabilities": "OK SNAPSHOT2",
    "personal-snapshot-capabilities": "OK PERSONAL1",
    "recovery-capabilities": "OK RECOVERY1",
    "layout-capabilities": "OK MAP1",
}
MISS = "/".join(["STKPWHRAO*EUFRPBLGTSDZ"] * 4)
LOOKUPS = ("KAT", "KAT/TA", "KAT/KAT", "PWOB", "STKPWHR", "A/ROUPBD", MISS)
PREVIOUS = {"convert": "baseline", "edit": "convert", "consolidate": "edit", "cold": "consolidate"}
WRITES = {"convert", "edit", "consolidate"}
MANUAL = ("USB typing from both halves and Javelin/Practice", "Unplugged BLE and split reconnect",
          "Sustained 200 WPM and burst output/timing", "Map and background maintenance while typing",
          "Priority and enabled-state exercise, then restore original settings", "Cold boot and sleep/wake timing",
          "Optional interrupted publication", "Separate expendable-spare USB recovery")


def now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def require(condition, message):
    if not condition:
        raise ValueError(message)


def verify_uf2(path):
    data = path.read_bytes()
    checkpoint = load(CHECKPOINT)
    digest = hashlib.sha256(data).hexdigest()
    for name, artifact in checkpoint["artifacts"].items():
        if digest == artifact["sha256"] and len(data) == artifact["bytes"]:
            return dict(commit=checkpoint["commit"], artifact=name, sha256=digest, bytes=len(data),
                        source=str(path), url=artifact["url"], verification="local UF2 file")
    raise ValueError("The UF2 does not match either pinned Gate B left artifact.")


def assert_serial(console, serial):
    require(console.serial == serial, "The connected HID serial does not match the selected spare.")


def state(console):
    installer = console.command("native_install status")
    require(installer.startswith("NATIVE2 READY "), installer)
    status = json.loads(console.command("user_dictionary_status"))
    require(status["ready"] and status["format"] == "SQBCAT02", "Native store is unavailable.")
    catalog = json.loads(console.command("native_catalog"))
    require([row["id"] for row in catalog] == status["ids"], "Catalog changed during capture. Inspect and retry idle.")
    return dict(installer=installer, status=status, catalog=catalog,
                host_layout=console.command("get_parameter host_layout"))


def personal_row(device):
    return next(row for row in device["catalog"] if row["name"] == PERSONAL)


def identities(catalog):
    return [(row["id"], row["name"], row["enabled"]) for row in catalog]


def export_file(out, filename, entries):
    path = out / filename
    save_atomic(path, dict(sorted(entries.items())))
    return dict(file=filename, count=len(entries), sha256=hashlib.sha256(path.read_bytes()).hexdigest())


def checked_export(out, export):
    path = out / export["file"]
    require(hashlib.sha256(path.read_bytes()).hexdigest() == export["sha256"], "Saved export hash changed.")
    return load(path)


def layout(console):
    try:
        return {"captured_at": now(), "result": "captured", "map": read_layout(console)}
    except RuntimeError as exc:
        if "Layout scan deferred" not in str(exc):
            raise
        return {"captured_at": now(), "result": "deferred", "reason": str(exc)}


def inspect(console, report):
    report["device"] = state(console)
    report["lookups"] = {key: console.command(f"lookup_stroke {key}") for key in LOOKUPS}
    report["layout"] = layout(console)


def baseline(console, out, report):
    report["capabilities"] = {name: console.command(f"native_install update {name}") for name in CAPABILITIES}
    require(report["capabilities"] == CAPABILITIES, "The matching experimental capabilities are required.")
    report["hello"] = console.command("hello")
    inspect(console, report)
    before = report["device"]
    backup = backup_all(console, complete=True)
    require(backup["format"] == FORMAT, "Complete backup version 2 required.")
    path = out / "before-bundle.json"
    save_atomic(path, backup)
    # Validate the actual saved file offline and derive all exports from that frozen view.
    image, view = reconstruct(load(path))
    report["backup"] = dict(file=path.name, file_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                            bundle_sha256=backup["sha256"], offline_verified=True)
    rows = [(struct.unpack_from("<I", row)[0], row_name(row), bool(row[4])) for row in view.rows]
    after = state(console)
    require(rows == identities(before["catalog"]) and after["catalog"] == before["catalog"] and
            backup["host_layout"] == before["host_layout"] == after["host_layout"] and
            before["status"]["generation"] == after["status"]["generation"],
            "Catalog or host layout changed around backup. Keep the bundle and capture a fresh baseline.")
    exports = report["exports"] = {}
    for identity, name, _ in rows:
        entries = effective_entries(view, name, lambda at, n: image[at:at + n])
        exports[name] = export_file(out, f"baseline-{identity}.json", entries)
    report["personal"] = exports[PERSONAL]


def require_previous(console, out, stage, report, origin, previous):
    before = state(console)
    report["before"] = before
    require(before["catalog"] == previous["device"]["catalog"] and
            before["host_layout"] == previous["device"]["host_layout"],
            "The live catalog, dictionary order, flags or host layout differ from the prior stage.")
    expected = checked_export(out, previous["personal"])
    require(export_dictionary(console, PERSONAL) == expected, "The live personal export differs from the prior stage.")
    if stage in WRITES:
        require(not before["status"]["read_only"], "Native store is read-only.")
    if stage == "convert":
        rows = before["catalog"]
        personal = personal_row(before)
        lapwing = next((row for row in rows if row["name"] == "Lapwing Base"), None)
        require(expected == {} and personal["kind"] == "personal" and personal["enabled"] and lapwing and
                lapwing["enabled"] and rows.index(personal) < rows.index(lapwing),
                "Conversion needs an empty enabled personal journal above enabled Lapwing Base on a disposable pool.")
        require(origin["lookups"]["KAT"] == "cat" and origin["lookups"]["A/ROUPBD"] == "around" and
                origin["lookups"][MISS] == "ERR No translation",
                "Factory fixture lookups must pass before converting the spare.")


def apply_fixture(console, stage, report, progress):
    if stage == "convert":
        entries, names = parse_named((FIXTURES / "seed-journal.json").read_bytes())
        fill, _ = parse_named((FIXTURES / "fill-personal.json").read_bytes())
        change_snapshot(console, "personal", PERSONAL, source=fill, progress=progress)
        report["write_may_have_run"] = True
        import_entries(console, personal_row(report["before"])["id"], entries, names, progress)
        progress("Seed saved. Reviewing the conversion with the actual journal state.")
        change_snapshot(console, "personal", PERSONAL, source=fill, progress=progress)
        return change_snapshot(console, "personal", PERSONAL, source=fill, stage=True, progress=progress)
    if stage == "edit":
        entries, names = parse_named((FIXTURES / "edit-personal.json").read_bytes())
        identity = personal_row(report["before"])["id"]
        report["write_may_have_run"] = True
        import_entries(console, identity, entries, names, progress)
        checked(console, f"native_remove_entry {identity} PWOB")
        return False
    change_snapshot(console, "consolidate", PERSONAL, progress=progress)
    report["write_may_have_run"] = True
    return change_snapshot(console, "consolidate", PERSONAL, stage=True, progress=progress)


def verify_stage(console, out, stage, report, origin):
    inspect(console, report)
    before, after = report["before"], report["device"]
    require(identities(after["catalog"]) == identities(before["catalog"]) and
            after["host_layout"] == before["host_layout"], "Dictionary identities, priorities or host layout changed.")
    require([row for row in after["catalog"] if row["name"] != PERSONAL] ==
            [row for row in before["catalog"] if row["name"] != PERSONAL], "Unrelated dictionary metadata changed.")
    require(personal_row(after)["kind"] == "snapshot", "The current personal dictionary is not compact.")
    if stage == "convert":
        expected = load(FIXTURES / "fill-personal.json") | load(FIXTURES / "seed-journal.json")
    else:
        expected = load(FIXTURES / "expected-after.json")
    actual = export_dictionary(console, PERSONAL)
    report["personal"] = export_file(out, f"{stage}-personal.json", actual)
    require(actual == expected, "The effective personal export differs from the exact fixture.")
    require(all(report["lookups"][key] == actual[key] for key in LOOKUPS if key in actual) and
            report["lookups"]["A/ROUPBD"] == origin["lookups"]["A/ROUPBD"] and
            report["lookups"][MISS] == "ERR No translation" and
            report["lookups"]["PWOB"] == actual.get("PWOB", origin["lookups"]["PWOB"]),
            "Forward lookup did not match the fixture or factory fallback.")
    if stage in ("consolidate", "cold"):
        count = overlay_usage(console)[personal_row(after)["id"]]["count"]
        report["personal_overlay_count"] = count
        require(count == 0, "Consolidation left live personal overlay records.")
    if stage == "cold":
        report["cold_boot"] = "Operator-supplied. Host checks persisted contents and catalog."


def run(stage, serial, out, uf2=None, confirmation=None):
    require(serial and (stage not in WRITES or serial == confirmation),
            "A write stage requires the exact USB serial and matching confirmation.")
    reports = out / (stage + ".json")
    if stage == "baseline":
        out.mkdir(mode=0o700)
    else:
        require(out.is_dir(), "Use the baseline output directory for later stages.")
    require(not reports.exists() or stage == "inspect", "Stage already completed. Keep its report.")
    report = dict(stage=stage, serial=serial, started_at=now(), write_may_have_run=False,
                  effect="external flash write" if stage in WRITES else "read-only",
                  outstanding_manual_checks=MANUAL, progress=[])
    if stage == "inspect":
        reports = out / f"inspect-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}.json"

    def progress(message):
        report["progress"].append(dict(at=now(), message=message))
        print(message, flush=True)

    try:
        if stage == "baseline":
            report["firmware"] = verify_uf2(uf2)
        if stage in PREVIOUS:
            origin = load(out / "baseline.json")
            previous = load(out / f"{PREVIOUS[stage]}.json")
            require(origin["serial"] == previous["serial"] == serial, "Serial differs from the captured baseline.")
            if stage in WRITES:
                bundle = origin["backup"]
                require(bundle["offline_verified"] and
                        hashlib.sha256((out / bundle["file"]).read_bytes()).hexdigest() == bundle["file_sha256"],
                        "The verified baseline backup is missing or changed.")
                require(not any(out.glob(f"{stage}-failed-*.json")),
                        "This write stage previously failed. Inspect the live state before any manual retry.")
        with open_console(serial) as console:
            assert_serial(console, serial)
            if stage == "baseline":
                baseline(console, out, report)
            elif stage == "inspect":
                inspect(console, report)
                actual = export_dictionary(console, PERSONAL)
                report["personal"] = export_file(out, f"{reports.stem}-personal.json", actual)
            else:
                require_previous(console, out, stage, report, origin, previous)
                reboot = apply_fixture(console, stage, report, progress) if stage in WRITES else False
        if stage in PREVIOUS:
            if reboot:
                wait_ready(serial)
            with open_console(serial) as console:
                assert_serial(console, serial)
                verify_stage(console, out, stage, report, origin)
        report["result"] = "pass"
    except (OSError, RuntimeError, ValueError, KeyError, StopIteration, KeyboardInterrupt) as exc:
        report.update(result="fail", error=f"{type(exc).__name__}: {exc}")
        reports = out / f"{stage}-failed-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}.json"
    report["finished_at"] = now()
    save_atomic(reports, report)
    print(reports, flush=True)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", nargs="?", default="baseline",
                        choices=("verify-uf2", "identify", "baseline", "inspect", "convert", "edit", "consolidate", "cold"))
    parser.add_argument("--serial", help="Exact USB HID serial of the expendable left")
    parser.add_argument("--out", type=Path, help="New private directory for baseline, then reuse it")
    parser.add_argument("--uf2", type=Path, help="Local checkpoint UF2 for verify-uf2 or baseline")
    parser.add_argument("--confirm-write", metavar="USB_SERIAL", help="Repeat the exact serial for each write stage")
    args = parser.parse_args()
    if args.stage in WRITES:
        if not args.serial or args.serial != args.confirm_write:
            parser.error("A write stage requires --serial and --confirm-write with the same serial.")
    elif args.confirm_write:
        parser.error("--confirm-write is only for convert, edit or consolidate.")
    if args.stage in ("verify-uf2", "baseline") and not args.uf2:
        parser.error("--uf2 is required to check the pinned artifact.")
    if args.stage == "verify-uf2":
        print(json.dumps(verify_uf2(args.uf2), indent=2))
        return 0
    if args.stage == "identify":
        with open_console(args.serial) as console:
            print(console.serial)
        return 0
    if not args.serial or not args.out:
        parser.error("--serial and --out are required.")
    result = run(args.stage, args.serial, args.out, args.uf2, args.confirm_write)
    if result["result"] != "pass":
        print(result["error"], file=sys.stderr)
        if result["write_may_have_run"]:
            print("Stop. Use inspect and retain the bundle. A write may already have committed.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, ValueError) as error:
        raise SystemExit(str(error)) from None
