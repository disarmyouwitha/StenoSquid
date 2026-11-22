# Offline StenoSquid Manager

Download and extract `boardsource.zip` or `nicenano.zip` for your board. Each
contains `Left.uf2`, `Right.uf2`, and `StenoSquidManager.html`. Open
**StenoSquidManager.html in desktop Chrome or Edge 137 or newer**. Connect the
left half and choose **Connect**. You can also drag the HTML file into Chrome.

The HTML contains the complete application, Python WebAssembly runtime, and
pinned factory dictionaries. **It works on first launch with no internet.**
There is no installation, Python dependency, local server, hosting, account,
extension, or browser flag. License notices are embedded in the HTML.
`stenosquid.py` stays in the repository for command-line use.

Safari and Firefox cannot connect directly because they do not support WebHID.
The Python Manager remains usable with those browsers. Use matching left firmware
that supports the `native_install connection` query; older firmware gets an
update message rather than guessing whether a connection is USB or Bluetooth.

The offline app uses the same UI and Python dictionary code as `stenosquid.py`:
imports, exports, edits, ordering, factory setup, compacting, merging, repacking,
flash maps, and guarded recovery all use the shared backend. USB-only operations
keep their existing restrictions. OS-paired Bluetooth depends on the browser and
OS exposing the keyboard's vendor HID console.

## JSON editor

Select an installed dictionary and choose **View / Edit JSON**. Edit the complete
effective dictionary, including saved overrides, as outline-to-translation JSON.
Matching factory dictionaries open from the bundled, checksum-verified JSON;
the keyboard supplies their saved overrides. Other dictionaries are read from
the device with byte progress. Loaded documents are cached until the keyboard's
catalog generation or journal tail changes. Reconnect and Reload explicitly
invalidate that cache. The whole-dictionary read also prepares Edits only from the same snapshot, so
switching to the filter does not transfer the dictionary again. Search finds
outlines or translations locally; Tab inserts spaces. Preview validates the
whole draft and shows added, changed, and removed entries. Save writes only those
changes to the selected dictionary. Factory images remain unchanged underneath
the saved overrides.

Use **Show → Edits only** to view saved overrides. Removing a key in this view
undoes that edit, exposing the compact baseline again; saving `{}` clears all
saved overrides for this dictionary. A `null` value represents a hidden entry:
remove that key to restore it. The whole-dictionary view keeps normal entry
removal behavior. Edits already folded into a compact baseline are part of that
baseline and no longer appear as separate overrides.

Imported compact dictionaries need left firmware advertising `OK RESET1` for
`snapshot-reset-capabilities` to undo saved overrides. Factory dictionaries work
with the existing reset command. The app checks support before writing anything.

Editor reads stop at the first unused journal record instead of transferring
the whole 64 KiB block. An empty journal needs one 512-byte read. Exact recovery
backups still read every referenced byte. Successful saves close the modal and
show “Dictionary changes saved.” on the main screen.

The editor rejects stale drafts if the keyboard dictionary changes. Copy a
draft before reloading if you want to keep it. An interrupted save reports how
many changes completed; reload from the keyboard before retrying.

## USB preference

**Connect** checks the connections you have already allowed in Chrome and prefers
USB for the same keyboard, falling back to Bluetooth when USB is unavailable.
Manager reads the identity and transport from firmware; it does not guess from
the device name. The connection badge and message show the selected transport.

Chrome's permission chooser uses the system device names and cannot be relabeled
by the app. **Choose device…** opens it when you need to authorize another
connection or pick a different keyboard. Grant USB access once to let Connect
prefer it in future. On the tested Boardsource pair, **Steno Squid** is the USB
name and **BLECORNE** is the Bluetooth name.

## Recovery backups

Before a USB repack or offline merge, choose a folder on your computer for
recovery files. The app waits for the original backup and plan files to finish
saving and reads them back before the shared recovery code can erase flash.
If a write or verification fails, the operation stops before erase.

To recover after closing the browser, select **Open recovery folder**, choose
that same folder, then **Connect**. Keep the recovery folder until you have
verified the keyboard. The files are ordinary local files, independent of browser
storage. Keep the tab open during writes, and close other dictionary tools.

## Build from source

Only maintainers need Python and internet access to obtain the build inputs:

```sh
python3 tools/dictionaries/prepare.py
python3 tools/ci/package_web_manager.py build/offline-manager
```

The builder verifies pinned dictionary hashes and a pinned Pyodide 0.29.3 runtime.
It emits the standalone HTML and offline ZIP. All inputs are embedded, and the
app's runtime loader serves bytes from the HTML itself. Unexpected network
requests are blocked. Subsequent builds can reuse the downloaded runtime:

```sh
python3 tools/ci/package_web_manager.py build/offline-manager --pyodide build/offline-manager/pyodide
```

Pyodide uses JavaScript Promise Integration to call asynchronous WebHID and local
file APIs from the existing synchronous Python code. No cross-origin isolation
headers, SharedArrayBuffer, or HTTP server are required.

## Verification

```sh
python3 tools/manager/test_manager.py
npm install --no-save playwright@1.62.1
npx playwright install chromium
node tools/manager/browser/test_browser.mjs build/offline-manager/StenoSquidManager.html
```

The browser test opens the actual HTML with `file://`, disables networking,
loads real Python WebAssembly, and drives the Manager through a simulated HID
device. It also runs 48 existing Python Manager and editor regression cases inside the
browser and checks local backup file safety. Physical-keyboard flash, reboot, Bluetooth, and recovery trials are still
required before publishing a release.
