# Manager over USB and Bluetooth

Start Manager with `python3 stenosquid.py`. Plug in the left half over USB
or pair the keyboard in your computer's Bluetooth settings, then click
**Connect**. Manager uses the existing HID pairing, shows the active
connection, and prefers USB when both are available.

| Operation | USB | Bluetooth |
| --- | --- | --- |
| Scan Memory, dictionary controls, entry edits, exports, full backup | Yes | Yes |
| Compact a dictionary, Compact Memory and Reclaim | Yes | Yes |
| Install or remove factory dictionaries | Yes | Yes. Large transfers show a speed warning. |
| Preview and import dictionary JSON | Yes | Requires USB |
| Preview USB Repack, Repack, and restore the original backup | Yes | Requires USB |

## Measured transfer speeds

Measured through the Manager's HID console on a Boardsource left with a Mac.
USB and Bluetooth returned matching checksums.

| Operation | USB | Bluetooth |
| --- | ---: | ---: |
| Read a 1,463-byte catalog, median | 26 ms | 361 ms |
| Read 64 KiB from flash | 41.0 KiB/s | 3.08 KiB/s |
| Upload 16 KiB to a staged extent, excluding erase | 39.5 KiB/s | 1.71 KiB/s |

The numbers measure console transfers. A complete install also includes
preparation, erasing, verification, and catalog publication. Separate
RAM-only probes reached roughly 15–16 KiB/s through binary GATT and L2CAP on
the test setup. They remained below production USB speeds and did not measure
flash installation or physical typing. Manager ships on the existing HID
transport.
