# Development checks

Use Python 3.10 or newer. From the repository root:

```sh
python3 -m pip install -r requirements-dev.txt
python3 -m ruff check
python3 tools/render_keymap.py --check
python3 tools/dictionaries/prepare.py
```

The normal keyboard setup tool needs only `hidapi`. Serial diagnostics additionally
need `pyserial`; artwork tools need Pillow. Native regression checks also need a
C/C++ compiler. `CC` and `CXX` can select it.

With a west workspace containing the pinned dependencies:

```sh
python3 tools/tests/run_all.py --workspace /path/to/workspace
```

Without an existing workspace, fetch the host-test dependencies as shown in
`.github/workflows/build.yml` and run `python3 tools/tests/run_all.py`.
Missing dependencies fail the checks rather than silently skipping them.
Use `--full` for the longer personal-dictionary power-loss model.

CI prepares pinned factory dictionaries, runs the full host suite and Linux
sanitizers, and builds eight firmware and probe variants. Version tags publish
the four everyday UF2s and the Manager ZIP after checks pass. Ordinary branch
pushes leave published releases in place.

## Capacity and typing

After preparing the pinned dictionaries, run the exact-byte compact capacity
gate and the paced typing replay from the repository root.

```sh
python3 tools/tests/test_native_compact_capacity.py
python3 tools/bench/run_typing_replay.py /path/to/west-workspace/javelin-steno --out build/typing-replay
```

The replay checks output after each stroke at 100, 150, and 200 WPM and in 300
WPM bursts. Add `--virtual` for a fast correctness run. Host timing covers
engine processing. Physical scanning, USB/BLE delivery, split behavior, and
maintenance contention require hardware checks. Use the
[physical checklist](archive/adaptive-snapshot-gate-a-b.md#physical-gate-b-checklist)
with current matching firmware. Run `python3 tools/bench/gate_b.py --help` for
the hardware harness. Its identification, inspection, and cold-readback stages
are read-only. Follow the [recovery guide](native-engine-integration.md#backup-and-usb-recovery)
for pool-erasing tests.
