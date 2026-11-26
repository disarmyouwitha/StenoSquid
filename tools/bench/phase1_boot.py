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


def collect_profile(console, report):
    events = report["profile"]
    index = events[-1]["index"] + 1 if events else 0
    while True:
        reply = console.command(f"native_install boot-profile {index}", timeout=2)
        if reply == "ERR Unknown native installation command":
            raise ValueError("The left firmware needs CONFIG_ZMK_STENO_SQUID_BOOT_PROFILE.")
        row = json.loads(reply)
        if events and row.get("uptime_ms", events[-1]["uptime_ms"]) < events[-1]["uptime_ms"]:
            # An nRF reset can occur during a USB reconnect. Index zero is
            # reused by the new firmware boot, so save the interrupted trace.
            report.setdefault("interrupted_profiles", []).append(events)
            events = report["profile"] = []
            index = 0
            continue
        if "stage" in row:
            events.append(row)
            index += 1
        elif row["first"] > index:
            report.setdefault("profile_gaps", []).append([index, row["first"]])
            index = row["first"]
        else:
            return


def check(serial, timeout, report, stop_on_unavailable=False):
    if usb_console_devices(serial):
        print("Waiting for the left half to reboot or be power-cycled.", flush=True)
        wait_for_usb(serial, False, timeout)
    start = wait_for_usb(serial, True, timeout)
    print("USB console appeared. Timing dictionary startup.", flush=True)
    while monotonic() - start < timeout:
        try:
            with open_console(serial) as console:
                if "profile" in report:
                    collect_profile(console, report)
                    latest = report["profile"][-1]["stage"] if report["profile"] else None
                    if stop_on_unavailable and latest == "flash_unavailable":
                        raise ValueError("Flash reported unavailable after validation.")
                    # The normal status command can wake idle flash. Observe the
                    # bring-up without requesting a wake until the flash is ready.
                    if latest != "flash_ready":
                        sleep(0.5)
                        continue
                status = console.command("native_install status", timeout=2)
                elapsed = monotonic() - start
                if not report["status"] or status != report["status"][-1]["reply"]:
                    report["status"].append(dict(seconds=round(elapsed, 3), reply=status))
                if status.startswith(("NATIVE2 UNPROVISIONED ", "NATIVE2 INSTALL ")):
                    raise ValueError(status)
                if status.startswith("NATIVE2 READY "):
                    reply = console.command("user_dictionary_status", timeout=5)
                    try:
                        native = json.loads(reply)
                    except json.JSONDecodeError as error:
                        raise RuntimeError(f"Dictionary status verification returned {reply!r}") from error
                    lookup = console.command("lookup_stroke KAT", timeout=5)
                    if not native["ready"] or lookup != "cat":
                        raise ValueError(f"Startup lookup failed: {lookup}")
                    if "profile" in report:
                        collect_profile(console, report)
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
    parser.add_argument("--profile", action="store_true", help="Collect firmware startup stages.")
    parser.add_argument("--stop-on-unavailable", action="store_true",
                        help="Save the trace immediately when validation leaves flash unavailable.")
    args = parser.parse_args()
    if args.timeout < 1:
        parser.error("--timeout must be positive")
    if args.stop_on_unavailable and not args.profile:
        parser.error("--stop-on-unavailable requires --profile")
    report = dict(result="fail", status=[])
    if args.profile:
        report["profile"] = []
    with args.report.open("x", encoding="utf-8") as stream:
        try:
            check(args.serial, args.timeout, report, args.stop_on_unavailable)
        except (RuntimeError, OSError, ValueError) as error:
            report["error"] = str(error)
            raise
        finally:
            json.dump(report, stream, indent=2)
            stream.write("\n")
    print(f"Javelin ready in {report['usb_to_ready_seconds']:.1f} seconds.")


if __name__ == "__main__":
    main()
