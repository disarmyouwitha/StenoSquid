#!/usr/bin/env python3
"""Test native catalog/index publication and live edits across a normal reboot.

Left-Diagnostic only, USB HID. Uses seven reserved sectors at FF9000..FFFFFF,
refuses nonblank test space, keeps the production dictionary format untouched.
The command holds the engine lock; do not type during the run.
"""

import argparse
import hashlib
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools/dictionaries"))
from import_personal import open_console


def invoke(console, command):
    response = console.command("dictionary_checkpoint_probe " + command, timeout=30)
    if response.startswith("ERR"):
        raise RuntimeError(response)
    result = json.loads(response)
    if result.get("passed") is not True:
        raise RuntimeError(f"Checkpoint {command} failed: {response}")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serial")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with open_console(args.serial) as console:
        metadata = json.loads(console.command("dictionary_benchmark meta"))
        before = console.command("lookup_stroke KAT")
        personal = console.command("print_dictionary user_dictionary", timeout=60)
        if personal.startswith("ERR") or before.startswith("ERR"):
            raise RuntimeError("Cannot verify production dictionary before test")
        args.output.with_suffix(".personal-backup.txt").write_text(personal + "\n")
        prepared = invoke(console, "hold")
        live_append = invoke(console, "live")
        watermark = json.loads(console.command("dictionary_benchmark meta"))
        response = console.command("dictionary_checkpoint_probe reboot")
        if response != "OK":
            raise RuntimeError("Normal reboot was not acknowledged: " + response)
    time.sleep(3)
    deadline = time.monotonic() + 45
    last_error = None
    while time.monotonic() < deadline:
        try:
            with open_console(args.serial) as console:
                restarted = json.loads(
                    console.command("dictionary_benchmark meta", timeout=2)
                )
            break
        except (OSError, RuntimeError, ValueError, TimeoutError) as error:
            last_error = str(error)
            time.sleep(0.5)
    else:
        raise RuntimeError("Keyboard did not return after reboot: " + str(last_error))
    with open_console(args.serial) as console:
        recovered = invoke(console, "check")
        live_recovered = invoke(console, "live_check")
        cleaned = invoke(console, "clean")
        if cleaned.get("cleaned") is not True:
            raise RuntimeError("Test regions were not verified clean")
        repeated = invoke(console, "run")
        if repeated.get("cleaned") is not True:
            raise RuntimeError("Repeat cleanup failed")
        after = console.command("lookup_stroke KAT")
        exported = console.command("print_dictionary user_dictionary", timeout=60)
        status = json.loads(console.command("user_dictionary_status"))
        final_metadata = json.loads(console.command("dictionary_benchmark meta"))
    report = dict(
        schema=2,
        metadata=metadata,
        prepared=prepared,
        live_append=live_append,
        live_recovered=live_recovered,
        after_prepare_metadata=watermark,
        after_reboot_metadata=restarted,
        recovered=recovered,
        cleanup=cleaned,
        repeated=repeated,
        final_metadata=final_metadata,
        production_status=status,
        unchanged_lookup=before == after,
        unchanged_personal=personal == exported,
        handler_sha256=hashlib.sha256(
            (ROOT / "src/steno_squid/checkpoint_probe.cc").read_bytes()
        ).hexdigest(),
        limits=[
            "Isolated seven-sector native catalog/index integration, not engine format replacement",
            "Normal software reboot, not power interruption during a write",
            "Ten edits and two dictionaries; large-store capacity and maintenance performance unproven",
            "Manual keyboard/display smoke tests deferred until owner returns",
        ],
    )
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    if (
        not report["unchanged_lookup"]
        or not report["unchanged_personal"]
        or status.get("integrity_errors")
    ):
        raise RuntimeError(
            "Production preservation check failed; inspect " + str(args.output)
        )
    print(
        json.dumps(
            {
                k: report[k]
                for k in (
                    "prepared",
                    "live_append",
                    "recovered",
                    "live_recovered",
                    "cleanup",
                    "repeated",
                )
            },
            indent=2,
        )
    )
    print(
        "PASS: native checkpoint reopened after reboot, test space cleaned, production dictionaries unchanged"
    )


if __name__ == "__main__":
    main()
