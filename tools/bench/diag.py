#!/usr/bin/env python3
"""Hardware-lab serial diagnostics for Left-Diagnostic builds (CONFIG_ZMK_JAVELIN_DIAG).

Not part of the Manager and not for everyday use: it reads the logging build's USB serial
port for status, the saved boot log, the rail test and the wake bench.

    python3 tools/bench/diag.py [PORT] [status|log|rail|bench|clear]   (no section = report)
Needs: python3 -m pip install pyserial
"""
import argparse
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "dictionaries"))
from serial_tools import find_port, LineReader  # noqa: E402

# Letter sent, line that ends the reply, and how long to wait for it (seconds): the rail
# test takes about ten and the power-cycle bench about half a minute.
DIAG = {
    "status": (b"S", "END", 20),
    "log": (b"D", "LOG END", 20),
    "clear": (b"Z", "CLEARED", 20),
    "rail": (b"R", "RAIL END", 40),
    "bench": (b"T", "BENCH END", 90),
}


def diag(port, what):
    """Print the firmware's status, saved boot log, or clear both. Never prompts."""
    command, end, wait = DIAG[what]
    port.reset_input_buffer()
    port.write(command)
    port.flush()
    deadline = time.monotonic() + wait
    reader = LineReader()
    while time.monotonic() < deadline:
        raw = reader.read(port)
        if raw is None:
            continue
        line = raw.decode("utf-8", errors="replace").rstrip()
        if line:
            print(line, flush=True)
        if line.startswith(end):
            return
    raise SystemExit("No diagnostics reply. This needs the logging build with "
                     "CONFIG_ZMK_JAVELIN_DIAG, which the Left-Diagnostic builds have")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("port", nargs="?", help="USB serial port (auto-detected when omitted)")
    parser.add_argument("section", nargs="?", default="report", choices=["report", *sorted(DIAG)])
    args = parser.parse_args()
    try:
        from serial import Serial
    except ImportError:
        raise SystemExit("Install the USB serial dependency with: python3 -m pip install pyserial") from None
    with Serial(args.port or find_port(), 115200, timeout=0.25, write_timeout=5, exclusive=True) as port:
        sections = ("status", "log", "rail", "bench") if args.section == "report" else (args.section,)
        for section in sections:
            if len(sections) > 1:
                print(f"== {section} ==", flush=True)
            diag(port, section)


if __name__ == "__main__":
    main()