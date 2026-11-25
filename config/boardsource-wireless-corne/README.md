# Boardsource Wireless Corne SMT bring-up

This port targets the integrated nRF52840 Boardsource Wireless Corne SMT pair. It uses the vendor's six-column matrix, split roles, bootloader partition layout, battery ADC, and USB configuration. The left runs the existing ZMK/Javelin central with external Lapwing and Plover bases. The right runs the split peripheral with nice!view and receives the active mode and Practice display state over Bluetooth.

## Expansion header

Number the connector by the vendor pinout and verify pin 1 on each physical board. Both halves have their own expansion rail. [Boardsource firmware and pinout](https://github.com/boardsource/wireless-corne_zmk_config/tree/2158104902e164c317e35f53dfa0ca1e36e1a751)

| Header pin | GPIO or rail | Left W25Q128 breakout | Right nice!view |
| --- | --- | --- | --- |
| 1 | P0.07 | CS# | |
| 2 | P0.21 | IO0 | |
| 3 | P0.12 | IO3 | MOSI |
| 4 | P0.23 | IO1 | SCK |
| 5 | Switched VCC, P0.31 control | 3V | VCC |
| 6 | GND | G | GND |
| 7 | P0.19 | CLK | CS, active high |
| 8 | P0.05 | IO2 | |

The [left QSPI overlay](qspi.dtsi) disables SPI2, uses high drive in both directions, pulls IO2 and IO3 high in the active state, and sets all six pins to low-power inputs with pulls disabled for sleep. The existing QSPI driver owns P0.31, powers up the flash before enabling its signal pins, and removes flash power during deep sleep. The right uses the vendor's SPI2 pinctrl and the standard ZMK external-power driver on P0.31. The pinned vendor devicetree declares SPI2 but has no P0.31 power driver. Both overlays use **active-low** rail enable.

On 2026-11-17–18, the left header measured 3.32 V on USB with P0.31 LOW and
approximately 0 V with it HIGH. The owner also measured approximately 3.3 V on
battery and approximately 3.3 V on USB with the flash breakout attached. USB and
battery/Bluetooth flash idle-off and immediate Javelin wake passed functionally.
The right display works with the same polarity setting, although its header was
not measured independently. Precise discharge timing and transient/load margin
remain unqualified. [Verified bringup record](../../docs/boardsource-bringup.md)

The owner locked the Steno Squid pin map on 2026-11-11. Clock uses Nordic’s recommended P0.19 QSPI/SCK pin. IO0 and IO1 use P0.21 and P0.23, which Nordic marks for QSPI. IO2 remains on P0.05, IO3 moves to P0.12, and CS# stays on P0.07. Both active and sleep pinctrl states use this map. Start with the 2 MHz probes on hand-wired setups. The Boardsource left firmware uses 32 MHz for the populated 16 MiB Steno Squid PCB. [Nordic nRF52840 pin assignments](https://docs.nordicsemi.com/bundle/ps_nrf52840/page/pin.html)

The Adafruit #5634 schematic routes IO2 and IO3 to separate pads. Check continuity on the actual breakout and confirm neither pad is shorted to VCC. Use eight jumper connections to the locked table above for the Adafruit proof of concept. Confirm orientation, pitch and clearance before mounting. [Adafruit breakout pinout](https://learn.adafruit.com/adafruit-qspi-dip-breakout-boards/pinouts) · [Adafruit breakout schematic](https://github.com/adafruit/Adafruit-QSPI-DIP-Breakout-PCB)

## Build

Use the repository's [pinned west manifest](../west.yml) and Zephyr SDK 0.16.9. After flashing either left build, follow the [Native provisioning instructions](../../README.md#provision-a-new-keyboard) if the external flash does not already have a Native pool.

Run from this repository's root in a shell with west and the Zephyr SDK available. Set `ws` to the absolute path of the pinned west workspace.

```sh
repo="$PWD"
target="$repo/config/boardsource-wireless-corne"
split="$repo/config/nicenano-wireless-corne"
probe="$repo/tools/qspi"
vendor="$repo/build/boardsource-upstream"

git clone https://github.com/boardsource/wireless-corne_zmk_config "$vendor"
git -C "$vendor" checkout --detach 2158104902e164c317e35f53dfa0ca1e36e1a751
export ZEPHYR_BASE="$ws/zephyr"
zephyr_cmake="$ZEPHYR_BASE/share/zephyr-package/cmake"
cd "$ws"

build_left() {
  output="$1"
  snippet="$2"
  shift 2
  conf="$split/corne.conf;$split/corne_left.conf"
  if [ "$snippet" = zmk-usb-logging ]; then
    conf="$conf;$split/logging.conf"
  fi
  west build -s zmk/app -d "$repo/build/$output" -b blecorne_left -S "$snippet" -- \
    "-DBOARD_ROOT=$vendor" "-DZephyr_DIR=$zephyr_cmake" \
    "-DZMK_CONFIG=$target" "-DZMK_EXTRA_MODULES=$repo" \
    "-DEXTRA_CONF_FILE=$conf" "$@"
}
build_left Boardsource-Left-Default studio-rpc-usb-uart
build_left Boardsource-Left-Diagnostic zmk-usb-logging \
  "-DEXTRA_DTC_OVERLAY_FILE=$target/logging.overlay"

west build -s zmk/app -d "$repo/build/Boardsource-Right-Default" -b blecorne_right -- \
  -DSHIELD=nice_view "-DBOARD_ROOT=$vendor" "-DZephyr_DIR=$zephyr_cmake" \
  "-DZMK_CONFIG=$target" "-DZMK_EXTRA_MODULES=$repo" \
  "-DEXTRA_CONF_FILE=$split/corne.conf;$split/right-nice-view.conf"

build_probe() {
  output="$1"
  shift
  west build -s "$probe" -d "$repo/build/$output" -b blecorne_left -- \
    "-DBOARD_ROOT=$vendor" "-DDTS_ROOT=$ws/zmk/app" "-DZephyr_DIR=$zephyr_cmake" \
    "-DDTC_OVERLAY_FILE=$probe/boardsource.overlay" \
    "-DKCONFIG_ROOT=$probe/Kconfig.boardsource" \
    "-DEXTRA_CONF_FILE=$probe/boardsource.conf" "$@"
}
build_probe Boardsource-Probe
build_probe Boardsource-Quad-Probe -DQUAD_PROBE=ON
```

| Output under `build/` | Use |
| --- | --- |
| `Boardsource-Probe/zephyr/zephyr.uf2` | First left-side probe. JEDEC and single-line FASTREAD only. |
| `Boardsource-Quad-Probe/zephyr/zephyr.uf2` | Follow-up left probe. Compares single-line and quad reads, using a volatile QE update when required. |
| `Boardsource-Right-Default/zephyr/zmk.uf2` | Right split peripheral with nice!view. |
| `Boardsource-Left-Default/zephyr/zmk.uf2` | Everyday left central, including browser HID over USB and Bluetooth. |
| `Boardsource-Left-Diagnostic/zephyr/zmk.uf2` | Left application with USB logging and diagnostic commands; dictionary provisioning uses USB HID. |

The factory application partition is `0x1000–0xD3FFF`. The settings partition starts at `0xD4000` and the UF2 bootloader starts at `0xF4000`. The vendor board keeps USB HID on the central and uses ADC channel 2 for its battery divider. This port shares the split keymap and settings in [nicenano-wireless-corne](../nicenano-wireless-corne), so ZMK Studio is on and starts unlocked, and battery test logging is left out. The everyday left build adds Studio over USB with the `studio-rpc-usb-uart` snippet; every left build has Studio over Bluetooth.

## Test on the new pair

1. **Establish recovery.** Save the matching [factory left and right UF2s](https://github.com/boardsource/wireless-corne_zmk_config), confirm stock typing and split pairing, and check that double-tap RESET mounts each integrated board's bootloader over USB. Record each `INFO_UF2.TXT` and flash one identified half at a time.
2. **Measure the empty header.** Confirm orientation, ground, and VCC with the accessories disconnected. Load the first left probe. Open its USB CDC serial port at 115200 with DTR asserted, which starts the probe and drives P0.31 LOW to enable the rail. Verify the enabled rail is suitable for 3.3 V flash and logic. Check rail rise and behavior on USB and battery, then repeat under load. Verify the nice!view rail and connector orientation separately. Once the Steno Squid is fitted with R1, the left diagnostic build can measure the same rail for you: run `python3 tools/bench/diag.py <port> rail` on USB and again on battery, and `bench` to power-cycle the flash ten times through the wake path (see the [diagnostics notes](../nicenano-wireless-corne/README.md)).
3. **Read the flash ID and sample.** With power off, attach the flash to the left header. Reboot, open the probe's serial port, and expect `JEDEC ID: EF 40 18` followed by a 16-byte `FASTREAD` at address zero. Compare against known bytes when available. An erased chip normally reads all `FF`. The first probe issues neither write-enable nor program/erase commands.
4. **Check quad reads.** Use the separate quad probe only after the rail and JEDEC checks. It compares standard SPI and quad reads over the first 64 KiB. Use a spare, independently preloaded reference chip with data exercising both bit states on all four lanes and look for a pass with lane mask `0xf`. An all-`FF` comparison is inconclusive. This build may temporarily set and restore the flash's volatile Quad Enable bit. It leaves the flash array alone.
5. **Test mapped reads and the split.** A reference flash with a provisioned Native store lets you test Javelin before any provisioning through the keyboard. Flash the matching right and everyday left UF2s. Open `StenoSquidManager.html` over USB: it reads the status without writing, and the keyboard reports `NATIVE2 READY`. Confirm ordinary typing on both halves, onboard `KAT` → “cat” with host Plover closed, raw Plover HID strokes, and nice!view mode and Practice updates. If you have only a blank flash, complete the electrical checks first and return to dictionary-dependent checks after initial provisioning.
6. **Test wireless and sleep.** Pair **Steno Squid** and unplug USB. Test ordinary typing, Plover HID, onboard Javelin, and Javelin Steno Tools **Connect via HID**. Let both halves reach deep sleep. Check that the left rail turns off, wake each half with one of its own keys, and check display and split reconnection, flash verification, and onboard output. On a diagnostic build, use `python3 tools/bench/diag.py <port> status` after USB reconnection to inspect the last Native open and wake timing. Check right display rail behavior and battery readings independently.
7. **Provision and test persistence.** Once power and patterned read checks pass,
   flash the everyday Boardsource left UF2, open `StenoSquidManager.html` over USB, and choose
   **Install selected dictionaries**. It installs Lapwing Base, its four extras, and Plover Base as separate images.
   Use `--verify` for a separate full CRC check and `--status` to confirm `NATIVE2 READY`.
   Switch bases in the browser, save a temporary personal edit, reboot, verify
   persistence, and remove the test entry. Repeat the HID and wake checks.

The wired 16 MB breakout passed recovery, rail polarity, patterned quad reads,
program/erase, USB HID dictionary installation with full CRC verification, both-half
typing, nice!view updates, reboot output, and flash idle/wake on USB and
battery/Bluetooth at 2 MHz. Full keyboard deep sleep, personal-entry persistence,
precise discharge timing, current/transient margin, battery calibration, final
populated-board fit and RF performance still need verification. See the
[bringup record](../../docs/boardsource-bringup.md) for evidence and limits.

## Steno Squid LED

The first adapter can leave the WS2812C-2020-V6 unpopulated. The left header uses all six signal pins for QSPI, and P0.31 controls the shared accessory rail. Keep the LED data off the flash signals. An independently powered or locally controlled LED could provide standalone effects, with its own power budget and voltage requirements. For firmware-controlled effects, a future board revision can expose a dedicated data pin and an appropriate LED supply. A simple rail indicator could instead use a low-current LED and resistor, at the cost of extra current whenever flash power is on.

## Builds

Use the Boardsource artifacts from one successful workflow run:

1. `Boardsource-Probe` for the initial rail, JEDEC and single-line read checks.
2. `Boardsource-Quad-Probe` for quad-read comparison after the initial checks pass.
3. `Boardsource-Right-Default` for the right half, with or without a nice!view.
4. `Boardsource-Left-Default` for everyday use. Connect the left half over USB and
   open `StenoSquidManager.html` and choose **Install selected dictionaries** to provision them.
   For an existing board, follow the [backup and update instructions](../../README.md#update-firmware).

The [32 MHz PCB qualification](../../docs/boardsource-bringup.md#32-mhz-qualification-on-the-16-mib-pcb)
covers the populated 16 MiB PCB. The wired Adafruit breakout results above
are from 2 MHz tests. The 32/64 MB flash variants still require qualification.
