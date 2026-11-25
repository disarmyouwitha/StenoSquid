#!/usr/bin/env python3
"""Measure USB-to-Javelin-ready time after a left-half reboot."""
import argparse
import json
from pathlib import Path
import sys
from time import monotonic, sleep

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "dictionaries"))
from import_personal import open_console, usb_console_devices


def wait_for_usb(serial, connected, timeout):
    deadline = monotonic() + timeout
    while monotonic() < deadline:
        if bool(usb_console_devices(serial)) == connected:
            return monotonic()
        sleep(0.2)
    raise TimeoutError(f"USB console did not {'appear' if connected else 'disappear'}")


def check(serial, timeout, report):
    if usb_console_devices(serial):
        print("Waiting for the left half to reboot or be power-cycled.", flush=True)
        wait_for_usb(serial, False, timeout)
    start = wait_for_usb(serial, True, timeout)
    print("USB console appeared. Timing dictionary startup.", flush=True)
    while monotonic() - start < timeout:
        try:
            with open_console(serial) as console:
                status = console.command("native_install status", timeout=2)
                elapsed = monotonic() - start
                if not report["status"] or status != report["status"][-1]["reply"]:
                    report["status"].append(dict(seconds=round(elapsed, 3), reply=status))
                if status.startswith(("NATIVE2 UNPROVISIONED ", "NATIVE2 INSTALL ")):
                    raise ValueError(status)
                if status.startswith("NATIVE2 READY "):
                    native = json.loads(console.command("user_dictionary_status", timeout=5))
                    lookup = console.command("lookup_stroke KAT", timeout=5)
                    if not native["ready"] or lookup != "cat":
                        raise ValueError(f"Startup lookup failed: {lookup}")
                    report.update(usb_to_ready_seconds=round(elapsed, 3),
                                  generation=native["generation"], dictionaries=native["dictionaries"],
                                  tail=native["tail"], lookup=lookup, result="pass")
                    return
        except (RuntimeError, OSError) as error:
            if not report["status"] or str(error) != report["status"][-1]["reply"]:
                report["status"].append(dict(seconds=round(monotonic() - start, 3), reply=str(error)))
        sleep(0.5)
    raise TimeoutError("Javelin did not become ready before the deadline")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serial", required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--timeout", type=int, default=600)
    args = parser.parse_args()
    if args.timeout < 1:
        parser.error("--timeout must be positive")
    report = dict(result="fail", status=[])
    with args.report.open("x", encoding="utf-8") as stream:
        try:
            check(args.serial, args.timeout, report)
        except (RuntimeError, OSError, ValueError) as error:
            report["error"] = str(error)
            raise
        finally:
            json.dump(report, stream, indent=2)
            stream.write("\n")
    print(f"Javelin ready in {report['usb_to_ready_seconds']:.1f} seconds.")


if __name__ == "__main__":
    main()
