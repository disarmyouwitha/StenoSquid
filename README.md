# StenoSquid

**Firmware for `Boardsource Wireless Corne SMT` && https://github.com/disarmyouwitha/StenoSquid-PCB** \
(But it should work with any nRF52840 `Nice!Nano` microcontroller + a 16 MiB `Adafruit #5634` QSPI breakout!)

✅ZMK Wireless Keyboard\
✅PloverHID\
✅Javelin onboard steno\
🤌🍇 Wireless dictionary updates\
🦑 Practice with just your keyboard!

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

Personal entries have highest priority, followed by the other dictionaries in order. Selections and personal edits survive reboot and firmware updates. Enable Plover Base and disable Lapwing dictionaries to switch bases. With every dictionary disabled, Javelin outputs literal outlines such as `KAT`.

## Practice Mode

From the Javelin or Plover layer, tap the outer right thumb

| Drill | Prompts | Checks |
| --- | ---: | --- |
| WORDS | 10 | Ten different words from a verified pool of 300 common Lapwing words, weighted toward mistakes, slow answers, and less-recent practice. |
| LEFT | 30 | Random left-hand keys. |
| RIGHT | 30 | Random right-hand keys. |
| COLUMNS | 30 | Paired keys: TK, PW, HR, FR, PB, LG, TS, DZ, AO, and EU. |
| Build-Up | 10 | Three growing chords per ladder, then a word reveal: K → KA → KA-T → CAT. |
| Multi- | 10 | Ten distinct two-stroke words from 20 verified common Lapwing outlines. |
| SPEED | 10 / 30 seconds | Ten distinct words from the 300-word pool, with immediate advancement and a live countdown. |
| TROUBLE | 10 | Focused repetitions of words your saved history marks as difficult. |

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

## Javelin Wireless Dictionary Management
1. Connect the left half by USB or Bluetooth!
2. Open Javelin Steno Tools in **Chrome**, choose **Connect via HID**, and select the keyboard.
3. Use **Active Dictionaries** to change selections and **Dictionary Management → View** to add, replace, remove, or export personal entries.
4. Use **Host Layout** to select **Mac U.S.** or **Windows U.S. (plain text)**. The keyboard remembers the choice.

**Windows U.S. (plain text)** is the default for a standard U.S. input layout. It uses ordinary U.S. keystrokes and converts accented Latin letters to plain equivalents, such as **ç → c**. (On a Mac, select **Mac U.S.** with the U.S. or ABC input source to type **aççents**.)

https://lim.au/#/software/javelin-steno-tools

## Build and install

⭐️ GitHub Actions builds these UF2 artifacts from the pinned dependencies. Install a left half with the right half that goes with it. **[Firmware builds](https://github.com/disarmyouwitha/StenoSquid/actions/workflows/build.yml)**

**Boardsource Wireless Corne SMT with the Steno Squid flash board**

| Artifact | Use |
| --- | --- |
| **Boardsource-Left-Default** | Everyday left half. Install it with **Boardsource-Right-Default**. |
| **Boardsource-Right-Default** | Right half with the portrait display, squid, stroke tape and Practice UI. |
| **Boardsource-Left-No-Display** | Left half for a keyboard without a nice!view. It sends the right half no status and Practice is off. Install it with **Boardsource-Right-No-Display**. |
| **Boardsource-Right-No-Display** | Right half without the display. |

**nice!nano v2** builds the same set with a `NiceNano-` prefix: `NiceNano-Left-Default` with `NiceNano-Right-Default`, or `NiceNano-Left-No-Display` with `NiceNano-Right-No-Display`.

### Provision a new keyboard

1. Flash the matching **Left** and **Right** UF2s. Use the **No-Display** pair if you don't have a nice!view.
2. Plug the left half in over USB, close any Javelin browser connection, and run these commands from this repository.

   ```sh
   python3 -m pip install hidapi
   python3 stenosquid.py --install
   ```

   The installer downloads and prepares missing dictionaries, writes them to the left half, and verifies them. You can use Javelin as soon as the installer finishes. If interrupted, reconnect USB and rerun the same command.

If the installer reports a conflict, run `python3 stenosquid.py --repair` to check
all configured base images and replace the ones that need repair.
The personal dictionary stays intact.

### Import a personal Plover dictionary

With the current **Left** firmware connected by USB, close the Javelin browser connection and run:

```sh
python3 stenosquid.py --import user.json
```

The file's translations win for matching outlines. Other personal entries stay in the dictionary. The importer verifies one complete snapshot before publishing it. Keep USB connected through verification and use a plain Plover JSON export. Plover-only formatting commands and plugins may behave differently in onboard Javelin.

Local builds must use this checkout's west manifest. If the workspace keeps a separate manifest copy, synchronize it before running `west update`. The compiled dictionary format depends on the pinned Javelin engine. [Dependency manifest](config/west.yml)

## External Memory Map
The compiled base and extras images occupy about 4.43 MiB of external flash. The personal dictionary is read directly from QSPI and has two 4 MiB snapshot slots. Saves compact the live entries and verify the replacement before switching to it.

![Left half QSPI flash map showing the two personal dictionary snapshots and 3.57 MiB of unallocated sectors](assets/qspi-flash-map.png)

The dark green sectors show a one-entry dictionary. The lighter green sectors are reserved for growth.

## NiceNano Wiring Diagram
![Wiring diagram for NiceNano](assets/NiceNano_wiring_diagram.png)

**Components used in handwired build**
* [Adafruit #5634 QSPI breakout](https://www.adafruit.com/product/5634)
* [10K ohm Resistor](https://www.amazon.com/dp/B07HDGX5LM)
* [100 nF ceramic capacitor](https://www.amazon.com/dp/B07X5XTDPB)

![Flash Wiring diagram](assets/NiceNano_Flash_wiring.png)

## License

The Javelin steno engine is licensed under PolyForm Noncommercial 1.0.0. Dependencies and dictionary sources retain their respective licenses. [Javelin license](https://github.com/jthlim/javelin-steno/blob/22726675b2dcf3b8b6ba1e20898f25e324b2260a/LICENSE.txt)
