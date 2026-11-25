#!/usr/bin/env python3
"""Phase 1 checks against the loaded 64 MiB hardware fixture."""
import argparse
import json
from pathlib import Path
from time import monotonic, sleep

from run_typing_replay import percentile
from import_personal import open_console
from native_layout import read_layout
from native_update import read_flash, update_command


def command_json(console, command):
    return json.loads(console.command(command, timeout=30))


def check_probes(console, fixture, report):
    report["probes"] = []
    for outline, expected, owner in fixture["probes"]:
        actual = console.command(f"lookup_stroke {outline}", timeout=30)
        report["probes"].append(dict(outline=outline, expected=expected, expected_owner=owner, actual=actual))
        if actual != (expected if expected is not None else "ERR No translation"):
            raise RuntimeError(f"Lookup differs for {outline}: {actual}")


def check(console, fixture, report):
    deadline = monotonic() + 60
    while monotonic() < deadline:
        try:
            report["benchmark_meta"] = command_json(console, "dictionary_benchmark meta")
            break
        except ValueError:
            sleep(0.5)
    else:
        raise RuntimeError("Phase 1 firmware and ready dictionaries are required")
    report["geometry"] = console.command("native_install geometry")
    if report["geometry"] != f"OK POOL1 {fixture['pool_segments']}":
        raise RuntimeError("Unexpected pool geometry")
    report["before"] = command_json(console, "user_dictionary_status")
    catalog = command_json(console, "native_catalog")
    report["catalog"] = catalog
    if len(catalog) != fixture["provisioned_slots"]:
        raise RuntimeError("Incorrect number of catalog rows")
    if [row["name"] for row in catalog if row["enabled"]] != fixture["active_names"]:
        raise RuntimeError("Active dictionary order differs from the fixture")
    report["layout"] = read_layout(console)
    if report["layout"]["free_segments"] != fixture["free_segments"]:
        raise RuntimeError("Occupied blocks differ from the fixture")
    report["integrity"] = console.command("user_dictionary_verify", timeout=180)
    if report["integrity"] != "OK":
        raise RuntimeError(report["integrity"])
    print("Full Native integrity passed on hardware.", flush=True)
    check_probes(console, fixture, report)
    print(f"Verified {len(report['probes'])} on-device lookups against the fixture.", flush=True)
    # Snapshotted bytes can be compared with the planned image directly.
    # Sample the first high-address extent of each kind, when present.
    update_command(console, "begin backup")
    try:
        report["high_address_samples"] = []
        with (Path(report["fixture"]).parent / "simulated-pool.bin").open("rb") as image:
            for kind in range(1, 5):
                extent = next((e for e in fixture["extents"] if e["kind"] == kind and e["start"] >= 256), None)
                if extent is None:
                    continue
                at = extent["start"] * 65536
                image.seek(at)
                matched = read_flash(console, at, 512) == image.read(512)
                report["high_address_samples"].append(dict(kind=kind, address=at, bytes=512, matched=matched))
                if not matched:
                    raise RuntimeError(f"High-address extent readback differs at 0x{at:08x}")
    finally:
        update_command(console, "abort", timeout=5)
    report["miss_bench"] = []
    for outline in fixture["full_stack_misses"]:
        sample = command_json(console, f"dictionary_benchmark lookup stack {outline}")
        if sample["hits"]:
            raise RuntimeError(f"The stack benchmark unexpectedly resolved {outline}")
        report["miss_bench"].append(dict(outline=outline, **sample))
    timer = report["benchmark_meta"]["timer_hz"]
    first = [(row["samples_cycles"][0] - row["bracket_cycles"]) * 1e6 / timer
             for row in report["miss_bench"]]
    warm = [(value - row["bracket_cycles"]) * 1e6 / timer for row in report["miss_bench"]
            for value in row["samples_cycles"][1:]]
    report["miss_latency_us"] = {
        label: {f"p{p}": percentile(values, p / 100) for p in (50, 95, 99)}
        | {"max": max(values)}
        for label, values in (("first_of_each_distinct_outline", first), ("within_outline_repeats", warm))
    }
    report["after"] = command_json(console, "user_dictionary_status")
    if report["after"]["input_overflows"] != report["before"]["input_overflows"]:
        raise RuntimeError("Keyboard reported input overflow")
    print("Rotating-miss benchmark and high-address readback complete.", flush=True)


def checkpoint(console, fixture, report):
    report["geometry"] = console.command("native_install geometry")
    if report["geometry"] != f"OK POOL1 {fixture['pool_segments']}":
        raise RuntimeError("Unexpected pool geometry")
    catalog = command_json(console, "native_catalog")
    if len(catalog) != fixture["provisioned_slots"] or [
            row["name"] for row in catalog if row["enabled"]] != fixture["active_names"]:
        raise RuntimeError("The defined fixture is not loaded")
    report["before"] = command_json(console, "user_dictionary_status")
    report["layout_before"] = read_layout(console)
    if report["layout_before"]["free_segments"] < fixture["stages_reserved"]:
        raise RuntimeError("The loaded pool has insufficient checkpoint staging space")
    started = monotonic()
    reply = console.command("user_dictionary_rebuild")
    if reply != "OK":
        raise RuntimeError(reply)
    while monotonic() - started < 600:
        after = command_json(console, "user_dictionary_status")
        if not after["maintenance"]:
            break
        sleep(0.5)
    else:
        raise RuntimeError("Checkpoint did not finish")
    report["seconds"] = monotonic() - started
    report["after"] = after
    if not after["ready"] or after["read_only"] or after["generation"] <= report["before"]["generation"]:
        raise RuntimeError("Checkpoint generation was not published as writable")
    report["layout_after"] = read_layout(console)
    report["index_blocks"] = {label: [row["index"] for row in report["layout_after"]["segments"]
                                      if row["kind"] == kind]
                              for label, kind in (("forward", "f"), ("reverse", "r"))}
    if any(not blocks or min(blocks) < 256 for blocks in report["index_blocks"].values()):
        raise RuntimeError("The replacement indexes did not land above 16 MiB")
    report["integrity"] = console.command("user_dictionary_verify", timeout=180)
    if report["integrity"] != "OK":
        raise RuntimeError(report["integrity"])
    check_probes(console, fixture, report)
    print("High-address checkpoint, full integrity and lookup oracle passed.", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--serial", required=True)
    parser.add_argument("--checkpoint", action="store_true", help="Run and verify a checkpoint on the loaded fixture")
    args = parser.parse_args()
    report = dict(fixture=str(args.fixture.resolve()), result="fail")
    try:
        fixture = json.loads(args.fixture.read_bytes())
        with open_console(args.serial) as console:
            operation = checkpoint if args.checkpoint else check
            operation(console, fixture, report)
        report["result"] = "pass"
    except Exception as error:
        report["error"] = str(error)
        raise
    finally:
        args.report.write_text(json.dumps(report, indent=2) + "\n")


if __name__ == "__main__":
    main()
