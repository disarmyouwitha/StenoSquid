#!/usr/bin/env python3
"""Verify the USB entry client preserves full UTF-8 and waits only on Busy."""

import base64
import json
from pathlib import Path
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools/storage"))
from native_manage import import_entries, commit_with_progress
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
        if command == "user_dictionary_status":
            return json.dumps(
                {"format": "SQBCAT02", "ready": True, "maintenance": 1, "work_steps": 1}
            )
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

    class Maintenance:
        def __init__(self, attempts, moving):
            self.attempts, self.moving, self.done = attempts, moving, 0

        def command(self, command, timeout=60):
            if command == "user_dictionary_status":
                return json.dumps(
                    {
                        "format": "SQBCAT02",
                        "ready": True,
                        "maintenance": 1,
                        "work_steps": self.done if self.moving else 0,
                    }
                )
            assert command == "native_entry commit"
            if self.done >= self.attempts:
                return "OK"
            self.done += 1
            return "ERR Native store busy; retry shortly"

    for attempts, moving, succeeds in (
        (5, True, True),
        (1000, False, False),
        (1000, True, False),
    ):
        elapsed = [0]

        def tick(_):
            elapsed[0] += 60

        with (
            patch("native_manage.time.monotonic", side_effect=lambda: elapsed[0]),
            patch("native_manage.time.sleep", side_effect=tick),
        ):
            try:
                commit_with_progress(
                    Maintenance(attempts, moving), progress=lambda message: None
                )
                assert succeeds
            except RuntimeError as error:
                assert not succeeds and "timed out" in str(error)
        if not succeeds:
            assert elapsed[0] == (3600 if moving else 180)
    print(
        "Native USB tools: full-length UTF-8, newline/empty text, fresh personal ID, progress-aware Busy retries, stall and total timeout bounds passed"
    )


if __name__ == "__main__":
    main()
