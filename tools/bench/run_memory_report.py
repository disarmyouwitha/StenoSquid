#!/usr/bin/env python3
"""Read heap and stack high-water marks from a Left build with CONFIG_ZMK_STENO_SQUID_MEMORY_DIAGNOSTICS.

Lab instrument, not part of the Manager. Flash a measurement build (append
config/boardsource-wireless-corne/memory.conf), run the workload under test (typing replay,
a transfer, Bluetooth traffic), then run this over USB. The marks cover everything since boot;
reboot to reset them. The raw JSON is saved so runs can be compared.

    python3 tools/bench/run_memory_report.py [--label NAME] [--serial USB_SERIAL] [--warn-free PERCENT]
"""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools/dictionaries"))


def analyse(report, warn_free=25.0):
    """Rows with used/free percentages, sorted tightest first, plus the heap summary."""
    rows = []
    for stack in report["stacks"]:
        size, used = stack["size"], stack["used"]
        free = size - used
        rows.append({**stack, "free": free, "free_pct": 100.0 * free / size if size else 0.0,
                     "warn": size > 0 and 100.0 * free / size < warn_free})
    rows.sort(key=lambda row: row["free_pct"])
    heap = report["heap"]
    summary = None
    if heap.get("ok"):
        summary = {**heap, "peak_pct": 100.0 * heap["max_allocated"] / heap["size"] if heap["size"] else 0.0,
                   "never_used": heap["size"] - heap["max_allocated"]}
    return rows, summary


def render(report, warn_free=25.0):
    rows, heap = analyse(report, warn_free)
    lines = [f"uptime {report['uptime_ms'] / 1000:.1f} s", "", f"{'stack':<22}{'size':>7}{'used':>7}{'free':>7}{'free %':>8}"]
    for row in rows:
        flag = "  <-- tight" if row["warn"] else ""
        lines.append(f"{row['name']:<22}{row['size']:>7}{row['used']:>7}{row['free']:>7}{row['free_pct']:>7.1f}%{flag}")
    lines.append("")
    if heap:
        lines.append(f"system heap: {heap['size']} B reserved, peak allocation {heap['max_allocated']} B "
                     f"({heap['peak_pct']:.1f}%), now {heap['allocated']} B; {heap['never_used']} B never used")
    else:
        lines.append("system heap: statistics unavailable (build without SYS_HEAP_RUNTIME_STATS?)")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--label", default="run", help="name for the saved report")
    parser.add_argument("--serial", help="USB keyboard serial number")
    parser.add_argument("--warn-free", type=float, default=25.0, help="flag stacks with less than this %% free")
    args = parser.parse_args()
    from import_personal import open_console

    with open_console(args.serial) as console:
        reply = console.command("memory_report", timeout=10)
    if not reply.startswith("{"):
        raise SystemExit(f"{reply}\nThis needs a USB connection and a build with "
                         "CONFIG_ZMK_STENO_SQUID_MEMORY_DIAGNOSTICS.")
    report = json.loads(reply)
    print(render(report, args.warn_free))
    folder = ROOT / "build/memory-report"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{args.label}.json"
    path.write_text(json.dumps(report, indent=1) + "\n")
    print(f"\nSaved {path.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
