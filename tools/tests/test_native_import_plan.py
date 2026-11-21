#!/usr/bin/env python3
"""Check compact import allocation, guarded publication and staging progress."""
from pathlib import Path
from contextlib import redirect_stderr
import io
import tempfile
from unittest.mock import patch
import zlib

from test_native_recovery import Console
from test_native_stage import fixture
from build_native_image import SEGMENT
from native_import_plan import consolidation_cost, metadata, named_import, plan_compact_import
from native_snapshot import compile_snapshot
from native_update import NoSpace, Update, publish


def inputs(n, value=None):
    return {(i // 1024 + 1, i % 1024 + 1): value if value is not None else f"term {i % 3000:04d}".encode()
            for i in range(n)}


class PlannerConsole(Console):
    serial = "fixture-left"
    def __init__(self, image, payload, protected):
        super().__init__(image, payload)
        self.protected = protected
        self.sessions = []
        self.reads = []
        self.generation = 3
        self.tail = 4

    def command(self, command, timeout=30):
        if command == "native_install update snapshot-capabilities":
            return "OK SNAPSHOT2"
        if command == "native_install update named-capabilities":
            return "OK NAMED1 16"
        if command == "user_dictionary_status":
            return f'{{"generation":{self.generation},"tail":{self.tail}}}'
        if command.startswith("native_install update read "):
            self.reads.append(int(command.split()[-2]))
        if command.startswith("native_install update begin "):
            assert command == "native_install update begin snapshot-named" and not self.active
            self.sessions.append(command)
            self.active = True
            words = ''.join(f"{sum(1 << (i % 32) for i in self.protected if i // 32 == part):08x}"
                            for part in range(8))
            return f"OK {SEGMENT + 32} {len(self.payload)} {zlib.crc32(self.payload)} {words}"
        return super().command(command, timeout)


def test_staging_progress():
    messages = []
    update = Update({2: b"A" * 4096, 8: b"\xff" * 512 + b"B" * 4096}, 8, b"B", 0, 0)
    with patch("native_update.update_command", return_value="OK Rebooting") as command:
        publish(None, update, messages.append)
    percentages = [int(message.split()[-1][:-1]) for message in messages[:-1]]
    assert percentages[0] == 0 and percentages[-1] == 100
    assert percentages == sorted(set(percentages))
    assert messages[-1] == "Verifying staged data and saving the catalog…"
    assert command.call_args.args[1].startswith("commit ")


def main():
    test_staging_progress()
    with tempfile.TemporaryDirectory() as folder:
        image, view, protected = fixture(Path(folder))
        name = "Preview"
        small = inputs(1)
        compiled = compile_snapshot(name, small)
        for entries in (small, inputs(10000)):
            console = PlannerConsole(image, view.payload, protected)
            preview = named_import(console, name, entries, progress=lambda _: None)
            assert preview["token"] and preview["entries"] == len(entries) and not console.active
            assert console.sessions == ["native_install update begin snapshot-named"]
            assert max(console.reads) < 2 * SEGMENT, "Compact preflight read journal data"
        console = PlannerConsole(image, view.payload, protected)
        preview = named_import(console, name, small, progress=lambda _: None)
        assert preview["image_segments"] == 1 and preview["staging_segments"] == 2
        approved = dict(stage=True, expected=preview["token"], progress=lambda _: None)
        with patch("native_import_plan.publish") as publish:
            different = {key: text.upper() for key, text in small.items()}
            for entries, tail, generation in ((different, 4, 3), (small, 5, 3), (small, 4, 4)):
                console.tail, console.generation = tail, generation
                try:
                    named_import(console, name, entries, **approved)
                except RuntimeError as error:
                    assert "plan changed" in str(error)
                else:
                    raise AssertionError("A different source or store passed the preflight token")
                assert not console.active
                publish.assert_not_called()
            console.tail, console.generation = 4, 3
            named_import(console, name, small, **approved)
            update = publish.call_args.args[1]
            assert not set(update.segments) & protected
            assert next(iter(update.segments.values()))[-12:-8] == b"SHI1"
            assert consolidation_cost(metadata(update.payload), 1, (8, 8)) == 4

        dense = {key: str(i).ljust(1120, "x").encode() for i, key in enumerate(inputs(6900))}
        with redirect_stderr(io.StringIO()):
            dense_image = compile_snapshot("Dense", dense)
        layout, initial = plan_compact_import(view, set(range(128)) | {255}, "Dense", dense_image, 16)
        assert initial and layout["staging_segments"] > 100
        try:
            consolidation_cost(metadata(initial.payload), layout["image_segments"], (1, 1))
        except NoSpace:
            pass
        else:
            raise AssertionError("Same-size consolidation unexpectedly fit")

        # Force the conservative bound into a cold recheck. A full journal
        # scan must use the exact counts before refusing publication.
        console = PlannerConsole(image, view.payload, protected)
        real_check = consolidation_cost
        calls = []

        def check(after, segments, counts):
            calls.append(counts)
            if len(calls) == 1:
                raise NoSpace("Conservative bound exceeded")
            return real_check(after, segments, counts)

        with patch("native_import_plan.consolidation_cost", side_effect=check):
            assert named_import(console, name, small, progress=lambda _: None)["token"]
        assert len(calls) == 2 and max(console.reads) >= 2 * SEGMENT
        assert calls[1] == (len(view.entries), sum(entry.operation == 0 for entry in view.entries.values()))

        with patch("native_import_plan.publish") as publish:
            limited = PlannerConsole(image, view.payload, set(range(251)) | {255})
            refused = named_import(limited, name, small, progress=lambda _: None)
            assert not refused["token"] and "reserve" in refused["reason"]
            try:
                named_import(limited, name, small, stage=True, progress=lambda _: None)
            except NoSpace:
                pass
            else:
                raise AssertionError("Full store incorrectly staged a snapshot")
            publish.assert_not_called()
    print("Compact-only import: metadata preview, approval, space checks, exact-count fallback and publish passed")


if __name__ == "__main__":
    main()
