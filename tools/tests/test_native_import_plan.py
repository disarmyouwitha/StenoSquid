#!/usr/bin/env python3
"""Check measured representation selection and staging preflight invariants."""
from pathlib import Path
from contextlib import redirect_stderr
import io
import tempfile
from unittest.mock import patch
import zlib

from test_native_recovery import Console
from test_native_stage import fixture
from build_native_image import SEGMENT
from native_import_plan import compare, named_import
from native_snapshot import compile_snapshot
from native_update import NoSpace


def inputs(n, value=None):
    return {(i // 1024 + 1, i % 1024 + 1): value if value is not None else f"term {i % 3000:04d}".encode()
            for i in range(n)}


class PlannerConsole(Console):
    def __init__(self, image, payload, protected):
        super().__init__(image, payload)
        self.protected = protected
        self.mode = None
        self.sessions = []
        self.refuse_journal = False

    def command(self, command, timeout=30):
        if command == "native_install update snapshot-capabilities":
            return "OK SNAPSHOT2"
        if command == "native_install update named-capabilities":
            return "OK NAMED1 16"
        if command.startswith("native_install update begin "):
            assert command.endswith(("snapshot-named", "named")) and not self.active
            self.mode = command.split()[-1]
            if self.refuse_journal and self.mode == "named":
                return "ERR Busy"
            self.sessions.append(self.mode)
            self.active = True
            words = ''.join(f"{sum(1 << (i % 32) for i in self.protected if i // 32 == part):08x}"
                            for part in range(8))
            return f"OK {SEGMENT + 32} {len(self.payload)} {zlib.crc32(self.payload)} {words}"
        return super().command(command, timeout)


def main():
    with tempfile.TemporaryDirectory() as folder:
        image, view, protected = fixture(Path(folder))
        small, large = inputs(100), inputs(10000)
        for data, expected in ((small, "journal"), (large, "snapshot")):
            report, updates = compare(view, protected, "Practice", data, 16, compile_snapshot("Practice", data))
            assert report["recommended"] == expected and report["reserve"] == 3
            for mode, update in updates.items():
                assert not set(update.segments) & protected
                assert report["options"][mode]["staging_segments"] == len(update.segments)
                assert report["options"][mode]["free_after_commit"] >= 3
            assert report["options"]["snapshot"]["eligibility"] == "copy-on-write"
        report, _ = compare(view, protected, "Unrepresentable", {(1 << 24,): b"value"}, 16,
                            None, "24-bit JSC4 limitation")
        assert report["recommended"] == "journal" and not report["options"]["snapshot"]["eligible"]
        assert report["options"]["snapshot"]["eligibility"] == "cannot-stage"
        dense = {key: str(i).ljust(1120, "x").encode() for i, key in enumerate(inputs(6900))}
        with redirect_stderr(io.StringIO()):
            dense_image = compile_snapshot("Dense", dense)
        restricted = set(range(128)) | {255}
        report, _ = compare(view, restricted, "Dense", dense, 16, dense_image)
        assert report["recommended"] is None
        assert "rebuild" in report["options"]["snapshot"]["reason"]
        assert report["options"]["snapshot"]["eligibility"] == "usb-rebuild-unqualified"
        assert report["options"]["snapshot"]["staging_segments"] > 100

        console = PlannerConsole(image, view.payload, protected)
        preview = named_import(console, "Preview", small)
        assert preview["selected"] == "journal" and not console.active
        different = {key: text.upper() for key, text in small.items()}
        with patch("native_import_plan.publish") as publish:
            try:
                named_import(console, "Preview", different, stage=True, expected=preview["token"])
            except RuntimeError as exc:
                assert "changed" in str(exc)
            else:
                raise AssertionError("A different source passed the preflight token")
            assert not console.active
            publish.assert_not_called()

            native = named_import(console, "Preview", small, preference="snapshot")
            assert native["selected"] == "snapshot"
            named_import(console, "Preview", small, stage=True, expected=preview["token"])
            assert console.mode == "named"
            publish.assert_called_once()
        console = PlannerConsole(image, view.payload, protected)
        console.refuse_journal = True
        with patch("native_import_plan.publish") as publish:
            try:
                named_import(console, "Preview", small, stage=True)
            except RuntimeError as exc:
                assert "Busy" in str(exc)
            else:
                raise AssertionError("Busy journal incorrectly staged")
            publish.assert_not_called()
        with patch("native_import_plan.publish") as publish:
            limited = PlannerConsole(image, view.payload, set(range(251)) | {255})
            try:
                named_import(limited, "Preview", small, preference="snapshot", stage=True)
            except NoSpace:
                pass
            else:
                raise AssertionError("Full store incorrectly allowed snapshot override")
            publish.assert_not_called()
    print("Adaptive import: measured recommendation, fragmented paths, overrides and preflight token passed")


if __name__ == "__main__":
    main()
