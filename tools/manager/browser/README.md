# Offline StenoSquid Manager

Download and extract `boardsource.zip` or `nicenano.zip` for your board. Each
contains `Left.uf2`, `Right.uf2`, and `StenoSquidManager.html`. Open
**StenoSquidManager.html in desktop Chrome or Edge**. Connect the
left half and choose **Connect**. You can also drag the HTML file into Chrome.

The HTML contains the complete application and the pinned factory dictionaries. **It works on first launch with no internet.**
There is no installation, runtime dependency, local server, hosting, account,
extension, or browser flag. License notices are embedded in the HTML.

Safari and Firefox cannot connect directly because they do not support WebHID.
Use matching left firmware
that supports the `native_install connection` query; older firmware gets an
update message rather than guessing whether a connection is USB or Bluetooth.

The Manager runs entirely in the page's own JavaScript: Connect and refresh, the flash map, enabling and disabling dictionaries, stroke
hints, the host layout, single-entry edits, the JSON editor (read, preview, save), import, export and the full backup, moving,
renaming and deleting dictionaries, compacting journals, converting and consolidating compact dictionaries, merging, named
imports, factory dictionary install/restore, first-time setup (the install image is built in the page from the bundled pinned
dictionaries), and guarded USB repack, offline merge and restore of the original backup (verified backups are kept in the
recovery folder you choose). Each operation was checked against the Python Manager it replaced, by recorded console transcripts and
byte-identical images and bundles (the recordings are in `golden/`).

USB-only operations keep their restrictions. OS-paired Bluetooth depends on the browser and OS exposing the keyboard's vendor HID
console.

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

The builder verifies the pinned dictionary hashes and joins the Manager's JavaScript modules, the page and the pinned
dictionaries into one standalone HTML file (plus an offline ZIP). Nothing else is embedded: there is no language runtime,
and any attempt to reach the network is blocked. No cross-origin isolation headers, SharedArrayBuffer or HTTP server are
required.

## Verification

```sh
node tools/manager/browser/run_tests.mjs
```

These tests need no browser and no keyboard. Most replay recordings made from the retired Python Manager
(`golden/*.json`): the same console commands, progress, results, failures, built images and recovery bundles. They are kept as
frozen recordings and are not part of CI. Physical-keyboard flash, reboot, Bluetooth and recovery trials are still required
before publishing a release.
