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

CI prepares and verifies the three dictionary images once, runs the native tests
and display renderer, then builds all 12 firmware/probe variants. It caches the
west dependencies by manifest hash. Actions, the build container and dependency
revisions are pinned. Version tags publish the eight everyday UF2s only after
all checks pass; ordinary branch pushes do not replace published releases.

The cleanup's protocol changes require matching left firmware, right firmware
and `stenosquid.py`. Practice progress starts fresh with its smaller block-based
format. Dictionary data, pairing settings and dictionary selections are separate.
