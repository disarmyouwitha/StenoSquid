#!/usr/bin/env python3
"""Verify the USB entry client preserves full UTF-8 and waits only on Busy."""

import base64
from pathlib import Path
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools/storage"))
from native_manage import import_entries
from import_personal import parse_named


class Console:
    def __init__(self):
        self.entries = []
        self.text = bytearray()
        self.size = 0
        self.outline = None
        self.busy = True

    def command(self, command, timeout=60):
        assert timeout > 0
        if command == "native_entry abort":
            return "OK"
        if command.startswith("native_entry begin "):
            _, _, identity, size, outline = command.split(" ", 4)
            assert identity == "8"
            self.text.clear()
            self.size, self.outline = int(size), outline
            return "OK"
        if command.startswith("native_entry data "):
            _, _, offset, encoded = command.split(" ", 3)
            assert int(offset) == len(self.text)
            self.text.extend(base64.b64decode(encoded, validate=True))
            return "OK"
        assert command == "native_entry commit"
        if self.busy:
            self.busy = False
            return "ERR Native store busy; retry shortly"
        assert len(self.text) == self.size
        self.entries.append((self.outline, bytes(self.text)))
        return "OK"


def main():
    source = '{"KAT":"ça va\\n ","HE/HROE":"","S":"' + "ç" * 1000 + "x" * 47 + '"}'
    entries, names = parse_named(source)
    console = Console()
    with patch("native_manage.time.sleep") as sleep:
        import_entries(console, 8, entries, names, progress=lambda message: None)
    assert sleep.call_count == 1
    assert console.entries == [(names[key], text) for key, text in entries.items()]
    try:
        parse_named('{"KAT":"one","KAT":"two"}')
    except ValueError:
        pass
    else:
        raise AssertionError("Duplicate keys must fail before opening the device")
    print(
        "Native USB tools: full-length UTF-8, newline/empty text, fresh personal ID and bounded Busy retries passed"
    )


if __name__ == "__main__":
    main()
