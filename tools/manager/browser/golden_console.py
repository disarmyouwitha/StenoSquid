#!/usr/bin/env python3
"""Generate (or check) golden vectors for the JavaScript vendor console (console.js, Manager milestone M3).

Oracle: the Console class in tools/dictionaries/import_personal.py, run on a deterministic fake clock and a scripted device.
Each case sends commands through a scripted device (the packets it answers with, in order) and records what the console wrote
(every report, byte for byte), what it returned or raised, and every sleep it asked for, so the JavaScript must behave the
same way on the same timeline.

    python3 tools/manager/browser/golden_console.py            # rewrite golden/console_vectors.json
    python3 tools/manager/browser/golden_console.py --check    # fail if the committed file is stale
"""
import json
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import golden_snapshot as base_vectors  # noqa: F401  (puts the tools/ directories on sys.path)
import import_personal
from import_personal import REPORT_BYTES, Console

OUT = HERE / "golden" / "console_vectors.json"
NL = chr(10)


class FakeTime:
    """A clock that only moves when the console waits or reads: 250 ms for an empty read, 2 ms for a packet."""

    def __init__(self):
        self.now, self.sleeps = 1000.0, []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class ScriptedDevice:
    """Answers reads from a per-command script; `{p}` in a packet stands for the channel prefix of the last write."""

    def __init__(self, clock, scripts):
        self.clock, self.scripts, self.index = clock, list(scripts), -1
        self.writes, self.reads, self.queue, self.prefix = [], 0, [], b""

    def begin(self):
        self.index += 1
        self.queue = list(self.scripts[self.index]) if self.index < len(self.scripts) else []

    def write(self, data):
        assert len(data) == REPORT_BYTES + 1 and data[0] == 0
        self.writes.append(bytes(data[1:]).hex())
        self.prefix = bytes(data[1:5])
        return len(data)

    def read(self, size, timeout):
        self.reads += 1
        if not self.queue:
            self.clock.now += timeout / 1000
            return b""
        item = self.queue.pop(0)
        if item is None:
            self.clock.now += timeout / 1000
            return b""
        self.clock.now += 0.002
        packet = (self.prefix if item["p"] else b"") + bytes.fromhex(item["hex"])
        return packet.ljust(REPORT_BYTES, b"\0") if item["pad"] else packet


def pk(text, p=True, pad=True):
    return {"p": p, "hex": text.encode("utf-8").hex(), "pad": pad}


def raw(hexbytes, p=True, pad=True):
    return {"p": p, "hex": hexbytes, "pad": pad}


TIMEOUT = None


def run_case(title, commands):
    """`commands`: [(text, timeout, script)]; the script is the packets served while that command waits."""
    clock = FakeTime()
    original = import_personal.time
    import_personal.time = clock
    try:
        device = ScriptedDevice(clock, [script for _, _, script in commands])
        console = Console(device)
        results = []
        for text, timeout, _ in commands:
            device.begin()
            try:
                results.append({"reply": console.command(text, timeout) if timeout is not None else console.command(text)})
            except Exception as error:
                results.append({"error": {"type": type(error).__name__, "message": str(error)}})
        record = {"title": title, "commands": [{"text": text, "timeout": timeout, "script": script} for text, timeout, script in commands],
                  "expect": {"results": results, "writes": device.writes, "reads": device.reads, "sleeps": clock.sleeps,
                             "clock": round(clock.now - 1000.0, 6), "channel": console.channel}}
    finally:
        import_personal.time = original
    return record


def generate():
    cases = []

    def add(title, commands):
        cases.append(run_case(title, commands))

    add("a one-packet reply", [("native_install status", None, [pk("NATIVE2 READY 7" + NL + NL)])])
    add("the reply split over packets", [("get_parameter host_layout", None, [pk("A" * 59, pad=False), pk("B" * 20 + NL + NL, p=False)])])
    add("a long reply over many packets", [("native_catalog", None, [pk("x" * 59, p=False if i else True, pad=False) for i in range(9)] + [pk(NL + NL, p=False)])])
    add("a reply with several trailing newlines", [("a", None, [pk("OK" + NL + NL + NL + NL)])])
    add("a reply with an interior blank line", [("a", None, [pk("one" + NL + NL + "two" + NL + NL)])])
    add("an empty reply", [("a", None, [pk(NL + NL)])])
    add("a reply of just a newline", [("a", None, [pk(NL)])])
    add("a reply that splits the terminator across packets", [("a", None, [pk("OK" + NL, pad=False), pk(NL, p=False)])])
    add("a stale reply from another channel is dropped", [("a", None, [pk("c99 old" + NL + NL, p=False), pk("fresh" + NL + NL)])])
    add("a stale reply then a timeout", [("a", 2, [pk("c99 old" + NL + NL, p=False)])])
    add("a packet with no prefix at all", [("a", 1, [pk("no prefix" + NL + NL, p=False)])])
    add("a prefix that only partly matches", [("a", 1, [pk("c0" + NL + NL, p=False)])])
    add("a reply that is exactly the prefix", [("a", None, [raw(("0a0a"))])])
    add("unicode in the reply", [("a", None, [pk("café 日本語 😀" + NL + NL)])])
    add("a multibyte character split across packets", [("a", None, [raw(bytes("日".encode()[:2]).hex(), pad=False), raw(bytes("日".encode()[2:]).hex() + "0a0a", p=False)])])
    add("an invalid UTF-8 reply", [("a", None, [raw("ff" + "0a0a")])])
    add("a packet of only padding is ignored", [("a", None, [raw("", p=False, pad=True), pk("late" + NL + NL)])])
    add("a short unpadded packet", [("a", None, [pk("OK" + NL + NL, pad=False)])])
    add("a timeout", [("native_install status", 1, [])])
    add("a timeout part way through a reply", [("a", 1, [pk("partial", pad=False)])])
    add("a timeout with explicit gaps", [("a", 1, [TIMEOUT, TIMEOUT, pk("late" + NL + NL)])])
    add("a reply after some empty reads", [("a", None, [TIMEOUT, TIMEOUT, TIMEOUT, pk("late" + NL + NL)])])
    add("a zero timeout", [("a", 0, [pk("OK" + NL + NL)])])
    add("a negative timeout", [("a", -1, [pk("OK" + NL + NL)])])
    add("a fractional timeout", [("a", 0.6, [])])
    add("an empty command that times out", [("", 1, [])])
    add("a whitespace command that times out", [("   ", 1, [])])
    add("a command with leading separators that times out", [(chr(0x1f) + "word rest", 1, [])])
    add("a command with a byte-order mark that times out", [(chr(0xfeff) + "word rest", 1, [])])
    add("a command with a non-breaking space that times out", [("native" + chr(0xa0) + "x y", 1, [])])
    for length in (1, 55, 58, 59, 60, 61, 117, 118, 119, 120, 177, 300):
        add(f"a command of {length} characters", [("c" * length, None, [pk("ok" + NL + NL)])])
    add("a multibyte command split across reports", [("日" * 40, None, [pk("ok" + NL + NL)])])
    add("a multibyte command at the report edge", [("a" * 57 + "日", None, [pk("ok" + NL + NL)])])
    add("a command with embedded newlines", [("one" + NL + "two", None, [pk("ok" + NL + NL)])])
    add("the channel advances", [(f"c{i}", None, [pk("r" + str(i) + NL + NL)]) for i in range(5)])
    add("the channel wraps after 99", [(f"n{i}", None, [pk("ok" + NL + NL)]) for i in range(103)])
    add("flash busy then ready", [("native_install status", None, [pk("ERR Native flash busy" + NL + NL), pk("ok" + NL + NL)])])
    add("flash busy twice then ready", [("native_install status", None, [pk("ERR Native flash busy" + NL + NL), pk("ok" + NL + NL)]),
                                        ("native_install status", None, [pk("ERR Native flash busy" + NL + NL), pk("ok" + NL + NL)])])
    add("flash busy three times then ready", [("native_install status", None, [pk("ERR Native flash busy" + NL + NL)] * 3 + [pk("ok" + NL + NL)])])
    add("flash busy until the deadline", [("native_install status", 1, [pk("ERR Native flash busy" + NL + NL)] * 40)])
    add("flash busy with a short deadline", [("native_install status", 0.05, [pk("ERR Native flash busy" + NL + NL)] * 5)])
    add("flash busy with a very short deadline", [("native_install status", 0.001, [pk("ERR Native flash busy" + NL + NL)] * 5)])
    add("flash busy for a command that is not native_install", [("native_catalog", None, [pk("ERR Native flash busy" + NL + NL)])])
    add("flash busy for a command that only starts like it", [("native_installx status", None, [pk("ERR Native flash busy" + NL + NL)])])
    add("flash busy for a prefix without the space", [("native_install", None, [pk("ERR Native flash busy" + NL + NL)])])
    add("flash busy with other text", [("native_install status", None, [pk("ERR Native flash busy now" + NL + NL)])])
    add("a different error is not retried", [("native_install status", None, [pk("ERR Something else" + NL + NL)])])
    add("busy then a timeout", [("native_install status", 0.5, [pk("ERR Native flash busy" + NL + NL), TIMEOUT, TIMEOUT, TIMEOUT])])
    add("busy replies that burn the whole budget", [("native_install update read 0 4", 0.3, [pk("ERR Native flash busy" + NL + NL)] * 10)])
    add("a reply with a newline but no blank line", [("a", 1, [pk("one" + NL + "two")])])
    add("a reply ending in a letter after a newline", [("a", 1, [pk("one" + NL + "t", pad=False)])])
    for code in (0x09, 0x0b, 0x0c, 0x0d, 0x1c, 0x1d, 0x1e, 0x85, 0x1680, 0x2000, 0x200a, 0x2028, 0x2029, 0x202f, 0x205f, 0x3000):
        add(f"a command led by U+{code:04X} that times out", [(chr(code) + "word rest", 1, [])])
    for code in (0x08, 0x0e, 0x1b, 0x200b, 0x2060, 0x3001, 0x180e):
        add(f"a command led by U+{code:04X} (a non-space) that times out", [(chr(code) + "word rest", 1, [])])
    add("a command after a timeout still works", [("a", 1, []), ("b", None, [pk("fine" + NL + NL)])])
    return {"schema": 1, "reportBytes": REPORT_BYTES, "cases": cases}


def main():
    text = json.dumps(generate(), indent=0) + chr(10)
    if "--check" in sys.argv:
        if not OUT.exists() or OUT.read_text(encoding="utf-8").replace(chr(13) + chr(10), chr(10)) != text:
            sys.exit("golden/console_vectors.json is stale; rerun golden_console.py")
        print("console vectors are current")
        return
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(text, encoding="utf-8", newline=chr(10))
    print(f"wrote {OUT} ({len(text) // 1024} KiB, {len(json.loads(text)['cases'])} cases)")


if __name__ == "__main__":
    main()
