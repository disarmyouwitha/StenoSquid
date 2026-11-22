# StenoSquid

✅ZMK Wireless Keyboard\
✅PloverHID\
✅Javelin onboard steno\
🤌🍇 Wireless dictionary editing\
🦑 Practice with just your keyboard!\
📚 Tested: 900,000+ total stored entries on 16 MiB!

**Firmware for `Boardsource Wireless Corne SMT` && https://github.com/disarmyouwitha/StenoSquid-PCB** \
(But it should work with any nRF52840 microcontroller + a 16 MiB `Adafruit #5634` QSPI breakout!)

![Pop](assets/screens/pop-hi.png) — ![Tape](assets/screens/tape-shortcut.png) — ![Practice](assets/screens/practice-menu.png) — ![BuildUp](assets/screens/drill-build-reveal.png) — ![XPk](assets/screens/results-xp.png)

[ZMK](https://github.com/zmkfirmware/zmk) handles the keyboard, Bluetooth, power management, and keymap editing. \
[Javelin](https://github.com/jthlim/javelin-steno) handles onboard steno translation.

## Keyboard modes

| Mode | Output |
| --- | --- |
| **Qwerty** | Ordinary keyboard input through ZMK. |
| **Numpad** | Arrow keys, a number pad, and Bluetooth controls. Hold the middle left thumb to use it. |
| **Javelin** | Onboard dictionary translation, including spelling rules, repeat, and undo. Works with Plover closed. |
| **Plover** | Raw steno strokes sent over Plover HID to Plover on the computer, using Plover's dictionaries. |
| **Practice** | Offline practice drills using only your keyboard, no PC required! |

## Onboard dictionaries

| Dictionary | Default |
| --- | --- |
| Personal dictionary | On |
| Lapwing Commands | On |
| Lapwing Numbers | On |
| Lapwing UK Additions | **Off** |
| Lapwing Proper Nouns | On |
| Lapwing Base | On |
| Plover Base | **Off** |

The full factory set contains 291,403 entries. A Boardsource test stored 918,000 compiled entries across 16 dictionaries on 16 MiB of flash. That total includes repeated dictionary copies and counts entries as stored, including matching outlines across dictionaries.

You can set active dictionary and priorities in [StenoSquid Manager](https://github.com/disarmyouwitha/StenoSquid#stenosquid-manager). Selections and personal edits survive reboot and firmware updates. With every dictionary disabled, Javelin outputs literal outlines such as `KAT`.

## Practice Mode

From the Javelin or Plover layer, tap the outer right thumb

| Drill | Prompts | Checks |
| --- | ---: | --- |
| WORDS | 10 | Ten different words from a verified pool of 300 common Lapwing words, weighted toward mistakes, slow answers, and less-recent practice. |
| LEFT | 30 | Random left-hand keys. |
| RIGHT | 30 | Random right-hand keys. |
| COLUMNS | 30 | Paired keys: TK, PW, HR, FR, PB, LG, TS, DZ, AO, and EU. |
| Build-Up | 10 | Build K → KA → KA-T, reveal CAT, then repeat KA-T with CAT visible. |
| Multi- | 10 | Ten distinct two-stroke words from 20 verified common Lapwing outlines. |
| SPEED | 10 / 30 seconds | Ten distinct words from the 300-word pool, with immediate advancement and a live countdown. |
| TROUBLE | 10 | Focused repetitions of words your saved history marks as difficult. |

## How to install

⭐️ **[Download firmware](https://github.com/disarmyouwitha/StenoSquid/releases)** —  Download the UF2s for your board and install the matching left and right halves.

### Provision a new keyboard

1. Flash the matching **Left** and **Right** UF2s for your board. Use the same pair with or without a nice!view. Practice uses the right display.
2. Plug the left half in over USB, close any Javelin browser connection, and run these commands from this repository.

   ```sh
   python3 -m pip install hidapi
   python3 stenosquid.py --install --confirm-erase
   ```

   This prepares the pinned dictionaries, replaces the external dictionary pool, and verifies the result. If a fresh install is interrupted, reconnect USB and rerun the same command. On a provisioned board, use `--install` without `--confirm-erase` to check that it is already installed.

If your board still has the shared Lapwing image from v0.1.2, export any dictionaries you want to keep as JSON before flashing. After flashing, use **Reprovision** in Manager to install the factory dictionaries, then import your exported JSON files.


## StenoSquid Manager

StenoSquid Manager offers full dictionary management! You can Import/Export, Edit, Rename and Delete dictionaries 

Download `StenoSquidManager.zip` from the release and extract it. Run these commands from the extracted folder to open Manager in your browser.

```sh
python3 -m pip install hidapi
python3 stenosquid.py
```

Plug in the left half or pair it in your computer’s Bluetooth settings and choose **Connect**. Manager prefers USB when both connections are available. Bluetooth requires matching left firmware and a recent version of `hidapi`. Manager shows which connection is active. JSON imports, **USB Repack**, and restoring a repack backup require USB.

<sub>We still offer compatibility with [Javelin Steno Tools](https://lim.au/#/software/javelin-steno-tools) for basic dictionary tools. </sub>

## Default Keymap 
Edit keymap with [ZMK Studio](https://github.com/disarmyouwitha/StenoSquid#zmk-studio)

### Qwerty

![Qwerty layer with tap and hold actions](assets/keymap/qwerty-3084f385c4c6.svg)

### Numpad

![Arrows and number pad layer with Bluetooth and Practice controls](assets/keymap/numpad-73da1dd8421b.svg)

### Javelin

![Onboard Javelin layer with Lapwing number key and Practice toggle](assets/keymap/javelin-68ed47c7832b.svg)

## ZMK Studio
ZMK Studio edits the keymap over USB (or Bluetooth with the Desktop app)!

Javelin and Plover HID bindings have separate parameters for the visible key label and the actual steno key. Change the steno identity to change what a key sends.

Saved Studio keymaps take precedence over the source keymap. To load updated defaults, use **Restore stock settings**, which replaces saved keymap edits. Renamed layers appear on the nice!view after a layer change. Only the first nine characters are sent across the split, and only plain ASCII.

https://zmk.studio/

## External Memory Map
Lapwing Base, Commands, Numbers, UK Additions, Proper Nouns, and Plover Base each have a separate compact image. **External flash map** in Manager shows the current layout. A fresh install with every factory dictionary leaves 178 free 64 KiB blocks, including the three-block maintenance reserve.

## NiceNano Wiring Diagram
![StenoSquid left-half matrix, flash and passive wiring](assets/StenoSquid_wiring.png)

**Components used in handwired build**
* [Adafruit #5634 QSPI breakout](https://www.adafruit.com/product/5634)
* [10K ohm Resistor](https://www.amazon.com/dp/B07HDGX5LM)
* [100 nF ceramic capacitor](https://www.amazon.com/dp/B07X5XTDPB)

The 10 kΩ resistor connects CS# to the flash's switched 3.3 V supply.
The 100 nF ceramic capacitor connects across flash VCC/GND. Both are nonpolarized.
Wire the Adafruit #5634's exposed IO2 and IO3 pads directly.
[Adafruit pinout](https://learn.adafruit.com/adafruit-qspi-dip-breakout-boards/pinouts)

[Technical documentation](docs/README.md) covers the storage design, hardware results, and development checks.

## License

StenoSquid’s own code is available under the [0BSD license](LICENSE).
The Javelin steno engine is licensed under PolyForm Noncommercial 1.0.0. Dependencies and dictionary sources retain their respective licenses. [Javelin license](https://github.com/jthlim/javelin-steno/blob/22726675b2dcf3b8b6ba1e20898f25e324b2260a/LICENSE.txt)
