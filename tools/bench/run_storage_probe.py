#!/usr/bin/env python3
"""Run the diagnostic native storage test in the reserved last 4 KiB sector.

Requires Left-Diagnostic on a provisioned 16 MiB keyboard over USB HID. The
firmware refuses a nonblank scratch sector; successful tests erase/verify only
that sector afterward. This does not switch Javelin to Candidate-B storage.
"""

import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools/dictionaries"))
from import_personal import open_console


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serial")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    with open_console(args.serial) as console:
        metadata = json.loads(console.command("dictionary_benchmark meta"))
        before = console.command("lookup_stroke KAT")
        personal = console.command("print_dictionary user_dictionary", timeout=60)
        if personal.startswith("ERR"):
            raise RuntimeError(
                "Cannot verify personal entries before probe: " + personal
            )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        # Private local backup, deliberately separate from any public report.
        backup = args.output.with_suffix(".personal-backup.txt")
        backup.write_text(personal + "\n")
        response = console.command("dictionary_storage_probe run", timeout=30)
        if response.startswith("ERR"):
            raise RuntimeError(response)
        result = json.loads(response)
        after = console.command("lookup_stroke KAT")
        restored = console.command("print_dictionary user_dictionary", timeout=60)
    report = dict(
        schema=1,
        metadata=metadata,
        result=result,
        unchanged_lookup=before == after and not before.startswith("ERR"),
        unchanged_personal=personal == restored,
        handler_sha256=hashlib.sha256(
            (ROOT / "src/steno_squid/storage_probe.cc").read_bytes()
        ).hexdigest(),
        limits=[
            "Five-record scratch-sector test, not a new engine/store installation",
            "Three-entry index; not 200000-entry hardware latency or near-full evidence",
            "Command holds engine lock during synchronous flash operations; do not type during run",
        ],
    )
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    if not (
        result.get("passed")
        and result.get("cleanup")
        and report["unchanged_lookup"]
        and report["unchanged_personal"]
    ):
        raise RuntimeError(f"Probe or preservation check failed; inspect {args.output}")
    print(json.dumps(result, indent=2))
    print("PASS: scratch cleaned; KAT lookup and personal dictionary unchanged")


if __name__ == "__main__":
    main()
