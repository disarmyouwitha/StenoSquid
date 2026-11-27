# Flashing lab builds without touching the board

A lab build can restart itself into the UF2 bootloader on request, so Claude, Codex or a script can flash the next build
without anyone double-tapping reset.

## How it works

* Firmware: `CONFIG_ZMK_STENO_SQUID_LAB_BOOTLOADER` adds the **USB-only** console command `native_install lab-bootloader`. It
  replies `OK Entering the UF2 bootloader`, then 300 ms later does what ZMK's `&bootloader` key does (retained boot mode plus a
  warm reboot). Dictionary flash and settings are untouched. Over Bluetooth the command does not exist.
* It is enabled by `config/boardsource-wireless-corne/lab-bootloader.conf`. The CI **Measure** builds (`*-Left-Measure`,
  `*-Right-Measure`) include it and the workflow fails if it is missing. Default and release builds do not have it, and must not.
* Host: `tools/bench/lab_flash.py` sends the command, waits for the bootloader drive (it carries `INFO_UF2.TXT`), copies the UF2 and
  waits for the keyboard to enumerate again. It works on Windows, macOS and Linux (needs `pip install hidapi`).

## Use it

1. Flash a Measure UF2 (from the CI run's artifacts, or a local build) onto the board **by hand once** (double-tap reset, copy the
   file). Any build with the command works as the starting point.
2. From then on:

```sh
python3 tools/bench/lab_flash.py path/to/NEXT.uf2            # one keyboard plugged in
python3 tools/bench/lab_flash.py NEXT.uf2 --serial 4F...     # pick one when both halves are plugged in
python3 tools/bench/lab_flash.py NEXT.uf2 --drive /Volumes/NICENANO   # board already in the bootloader
```

3. A build **without** the command must be flashed by hand again, and the script says so. Always keep a Measure (or other
   lab-bootloader) build as the thing you flash next, or the autonomy ends.

## Local builds

`tools/ci/build_local_boardsource.sh OUT [extra conf;] [overlay]` mirrors the CI Boardsource recipe in the WSL workspace.
Append `config/boardsource-wireless-corne/lab-bootloader.conf` to the extra conf. For the handwired nice!nano the same fragment
goes on the `EXTRA_CONF_FILE` list of `~/steno-ws/build_nn.sh`.

## Rules

* Only flash a board the owner has agreed to flash. The 64 MiB Boardsource is the owner's daily driver: ask before touching it.
* Back up before any test that writes to the keyboard (the Manager's full backup, or `--backup-to` on the upload tools), and leave
  the keyboard with its dictionaries installed and enabled when you finish.
* Hardware status: proven on the handwired nice!nano (Adafruit UF2 bootloader), about 15 s per flash including re-enumeration.
  **Not yet proven on the Boardsource Left**; if the board does not enter the bootloader, flash by hand and report it.
